# scripts/ingest_seaweedfs.py
import os
import re
import gc
import asyncio
import logging
import traceback
import aioboto3
from botocore.exceptions import ClientError
from botocore.config import Config
from datetime import datetime, timezone
from typing import List, Dict, Any

from sqlalchemy import select, create_engine
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from qdrant_client import AsyncQdrantClient, QdrantClient
from qdrant_client.models import CreateAliasOperation, CreateAlias, FieldCondition, Filter, FilterSelector, MatchValue
from redis.asyncio import Redis

from app.core.config import settings
from app.models.traceability import InventarioArchivo, ErrorLog
from app.services.extractor import extraer_desde_pdf
from app.services.extractor_ficha import extraer_ficha

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("atv.ingestion")

STAGING_DIR = "/var/www/aldeazon-atv/fixtures/staging"
EMBEDDING_MODEL = "intfloat/multilingual-e5-large"
ALIAS_NAME = settings.qdrant_collection
CURRENT_CORPUS_VERSION = 1

# NOTE: previously this was a separate hardcoded constant ("atv_corpus_v1")
# that only happened to match f"{ALIAS_NAME}_v{CURRENT_CORPUS_VERSION}" by
# coincidence. Deriving it directly removes that landmine.
COLLECTION_NAME = f"{ALIAS_NAME}_v{CURRENT_CORPUS_VERSION}"

# Desde v2 la extracción es por ficha (app/services/extractor_ficha.py). La
# ingesta diaria mantiene la versión a la que apunta HOY el alias (ver
# version_vigente): no vuelve a v1 cuando se promueve v2 con
# scripts/reindexar_corpus.py, ni promueve v2 antes de validarla.
VERSION_EXTRACCION_POR_FICHA = 2

# Batch size for periodic Qdrant flush. We upsert per-file (see below) but
# this caps how many text/metadata/id lists we keep resident at once if a
# single file yields an unusually large number of tuples.
UPSERT_BATCH_SIZE = 64

offline_async_engine = create_async_engine(settings.database_url, poolclass=NullPool)
OfflineAsyncSession = async_sessionmaker(offline_async_engine, expire_on_commit=False)

offline_sync_engine = create_engine(settings.database_url, poolclass=NullPool)
OfflineSyncSession = sessionmaker(bind=offline_sync_engine)


async def sincronizar_seaweedfs() -> None:
    logger.info(f"Connecting to SeaweedFS S3 at {settings.s3_endpoint_url}...")
    os.makedirs(STAGING_DIR, exist_ok=True)
    session = aioboto3.Session()

    custom_s3_config = Config(s3={'addressing_style': 'path'}, signature_version='s3v4')

    async with session.client('s3',
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=custom_s3_config
    ) as s3:
        try:
            # SCOPED S3 FETCH
            response = await s3.list_objects_v2(
                Bucket=settings.s3_bucket_corpus, 
                Prefix="raw/ostendata/aldeazon-atv/Fichas Técnicas de Productos/"
            )
            remotos = response.get('Contents', [])
        except ClientError as exc:
            logger.error(f"S3 Connection Failed: {exc}")
            return

        async with OfflineAsyncSession() as db:
            query = await db.execute(select(InventarioArchivo))
            inventario_local = {rec.nombre_archivo: rec for rec in query.scalars().all()}
            remotos_nombres = set()

            for obj in remotos:
                nombre = obj['Key']
                
                if nombre.endswith('/') or not nombre.lower().endswith('.pdf'):
                    continue

                etag = obj['ETag'].strip('"')
                remotos_nombres.add(nombre)
                registro = inventario_local.get(nombre)

                filename_local = os.path.basename(nombre)
                filepath = os.path.join(STAGING_DIR, filename_local)

                archivo_falta_en_disco = not os.path.exists(filepath)
                descargar = False

                if not registro:
                    logger.info(f"New technical PDF detected: {nombre}")
                    nuevo_reg = InventarioArchivo(
                        nombre_archivo=nombre,
                        etag_seaweedfs=etag,
                        version_corpus=CURRENT_CORPUS_VERSION,
                        estado="NUEVO"
                    )
                    db.add(nuevo_reg)
                    descargar = True
                elif registro.etag_seaweedfs != etag:
                    logger.info(f"Modified technical PDF detected: {nombre}")
                    registro.etag_seaweedfs = etag
                    registro.estado = "MODIFICADO"
                    descargar = True
                elif archivo_falta_en_disco:
                    logger.info(f"Missing local binary on disk, queueing download: {filename_local}")
                    descargar = True

                if descargar:
                    try:
                        logger.info(f"Downloading stream for {filename_local}...")
                        await s3.download_file(settings.s3_bucket_corpus, nombre, filepath)
                    except Exception as e:
                        logger.error(f"I/O error downloading '{nombre}': {e}")
                        if registro:
                            registro.estado = "ERROR"

            for nombre, registro in inventario_local.items():
                if nombre not in remotos_nombres and registro.estado != "RETIRADO":
                    registro.estado = "RETIRADO"

            await db.commit()
            logger.info("SeaweedFS sync & binary downloads complete.")


def version_vigente(qdrant: QdrantClient) -> int:
    """Versión de la colección a la que apunta el alias (atv_corpus -> atv_corpus_vN)."""
    for alias in qdrant.get_aliases().aliases:
        if alias.alias_name == ALIAS_NAME:
            if m := re.fullmatch(rf"{re.escape(ALIAS_NAME)}_v(\d+)", alias.collection_name):
                return int(m.group(1))
    return CURRENT_CORPUS_VERSION


def reemplazar_registros_documento(qdrant: QdrantClient, coleccion: str, filename: str, registros) -> None:
    """Borra los puntos del documento en la colección y sube los nuevos (re-extraer no duplica)."""
    qdrant.delete(
        collection_name=coleccion,
        points_selector=FilterSelector(filter=Filter(must=[
            FieldCondition(key="documento_fuente", match=MatchValue(value=filename))
        ])),
    )
    for i in range(0, len(registros), UPSERT_BATCH_SIZE):
        lote = registros[i:i + UPSERT_BATCH_SIZE]
        qdrant.add(
            collection_name=coleccion,
            documents=[r.texto_para_embedding() for r in lote],
            metadata=[r.model_dump() for r in lote],
            ids=[r.registro_id for r in lote],
            batch_size=8,  # textos largos (fichas completas): lotes chicos = menos RAM de ONNX
        )


def _ensure_qdrant_collection(qdrant: QdrantClient, coleccion: str = COLLECTION_NAME) -> None:
    """
    Idempotent collection setup.

    IMPORTANT: unlike the previous version, this does NOT drop/recreate the
    collection unconditionally. If the collection already exists (e.g. the
    previous run was killed partway through), we leave it alone so that
    points already upserted survive a restart. The collection is only
    created fresh the first time it's missing.
    """
    if not qdrant.collection_exists(coleccion):
        logger.info(f"Collection '{coleccion}' does not exist yet. Creating it.")
        qdrant.create_collection(
            collection_name=coleccion,
            vectors_config=qdrant.get_fastembed_vector_params(),
        )
    else:
        logger.info(f"Collection '{coleccion}' already exists. Upserting incrementally.")


def reconstruir_indice_qdrant() -> int:
    """Indexa los PDF pendientes en la colección vigente y devuelve su versión (la que hay que promover)."""
    db = OfflineSyncSession()

    if not os.path.exists(STAGING_DIR):
        logger.error(f"Staging directory '{STAGING_DIR}' not found. Aborting index.")
        db.close()
        return CURRENT_CORPUS_VERSION

    archivos_staging = [f for f in sorted(os.listdir(STAGING_DIR)) if f.lower().endswith(".pdf")]
    logger.info(f"Found {len(archivos_staging)} PDF(s) in staging for extraction.")

    qdrant = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    qdrant.set_model(EMBEDDING_MODEL)
    version = version_vigente(qdrant)
    coleccion = f"{ALIAS_NAME}_v{version}"
    por_ficha = version >= VERSION_EXTRACCION_POR_FICHA
    logger.info(f"Corpus vigente: v{version} ('{coleccion}'), extracción {'por ficha' if por_ficha else 'por línea'}.")
    _ensure_qdrant_collection(qdrant, coleccion)

    total_indexado = 0
    total_omitido = 0

    for filename in archivos_staging:
        filepath = os.path.join(STAGING_DIR, filename)

        # MLOps: Prevent MultipleResultsFound by using scalars().first()
        inv_entry = db.execute(
            select(InventarioArchivo)
            .where(InventarioArchivo.nombre_archivo.endswith(filename))
            .order_by(InventarioArchivo.ts_cargado.desc())
        ).scalars().first()

        if inv_entry and inv_entry.estado == "ERROR":
            logger.warning(f"Skipping known error file: {filename}")
            continue

        # RESUMABILITY: if this file was already successfully indexed in a
        # prior run (its etag hasn't changed since, otherwise
        # sincronizar_seaweedfs() would have flipped it to "MODIFICADO"),
        # don't re-extract or re-upsert it. This is what makes it safe to
        # simply re-run this script after an OOM kill.
        if inv_entry and inv_entry.estado == "INDEXADO":
            logger.info(f"Already indexed, skipping: {filename}")
            total_omitido += 1
            continue

        lote_textos: List[str] = []
        lote_metadata: List[Dict[str, Any]] = []
        lote_ids: List[str] = []

        try:
            extracted_count = 0
            logger.info(f"Extracting atomic records from: {filename}...")

            if por_ficha:
                resultado = extraer_ficha(filepath, filename, version)
                if resultado.error:
                    raise RuntimeError(resultado.error)
                reemplazar_registros_documento(qdrant, coleccion, filename, resultado.registros)
                extracted_count = len(resultado.registros)

            for registro in ([] if por_ficha else extraer_desde_pdf(filepath, filename)):
                lote_textos.append(registro.texto_para_embedding())
                lote_metadata.append(registro.model_dump())
                lote_ids.append(registro.registro_id)
                extracted_count += 1

                # Flush in sub-batches so a single huge PDF doesn't hold
                # everything in memory before hitting Qdrant.
                if len(lote_ids) >= UPSERT_BATCH_SIZE:
                    qdrant.add(
                        collection_name=COLLECTION_NAME,
                        documents=lote_textos,
                        metadata=lote_metadata,
                        ids=lote_ids,
                    )
                    lote_textos, lote_metadata, lote_ids = [], [], []

            # Flush whatever remains for this file (< UPSERT_BATCH_SIZE).
            if lote_ids:
                qdrant.add(
                    collection_name=COLLECTION_NAME,
                    documents=lote_textos,
                    metadata=lote_metadata,
                    ids=lote_ids,
                )

            logger.info(f"Extracted and upserted {extracted_count} atomic tuples from '{filename}'.")

            if inv_entry:
                inv_entry.total_registros_atomicos = extracted_count
                inv_entry.estado = "INDEXADO" if extracted_count > 0 else "ERROR"
                inv_entry.ts_indexado = datetime.now(timezone.utc)
                db.commit()

            if extracted_count > 0:
                total_indexado += 1

        except Exception as e:
            logger.error(f"Extraction failed for '{filename}': {e}")
            db.rollback()

            error_entry = ErrorLog(
                componente="ingest_seaweedfs (Airflow DAG)",
                excepcion_tipo=type(e).__name__,
                mensaje=f"Fallo extrayendo PDF '{filename}': {str(e)[:200]}",
                stack_trace=traceback.format_exc(),
                resuelto=False
            )
            db.add(error_entry)

            if inv_entry:
                inv_entry.estado = "ERROR"

            db.commit()
            continue

        finally:
            # The VISION fallback path renders pages to images and calls
            # out to the OpenAI API, which is the most memory-hungry part
            # of this pipeline. Explicitly drop per-file buffers and force
            # a collection so memory doesn't accumulate across the loop
            # (this is what was almost certainly driving the OOM kill).
            del lote_textos, lote_metadata, lote_ids
            gc.collect()

    db.close()

    if total_indexado == 0 and total_omitido == 0:
        logger.error("[CRITICAL] Extraction finished with 0 chunks indexed and 0 skipped. Qdrant index may be empty.")
        return version

    logger.info(
        f"Indexing pass complete. {total_indexado} file(s) newly indexed, "
        f"{total_omitido} file(s) already up to date and skipped."
    )
    return version


async def promover_indice_y_purgar_cache(version_nueva: int) -> None:
    coleccion_nueva = f"{ALIAS_NAME}_v{version_nueva}"
    qdrant = AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)

    # Si existe una colección cruda con el mismo nombre que el alias, eliminarla para evitar colisión
    aliases_resp = await qdrant.get_aliases()
    existing_aliases = {a.alias_name: a.collection_name for a in aliases_resp.aliases}
    if ALIAS_NAME not in existing_aliases and await qdrant.collection_exists(ALIAS_NAME):
        logger.warning(f"Colección cruda '{ALIAS_NAME}' detectada sin alias. Eliminándola para adjuntar alias hacia '{coleccion_nueva}'...")
        await qdrant.delete_collection(ALIAS_NAME)

    logger.info(f"Switching alias '{ALIAS_NAME}' to '{coleccion_nueva}'...")
    await qdrant.update_collection_aliases(
        change_aliases_operations=[
            CreateAliasOperation(
                create_alias=CreateAlias(
                    collection_name=coleccion_nueva,
                    alias_name=ALIAS_NAME
                )
            )
        ]
    )

    redis_client = Redis.from_url(settings.redis_url)
    await redis_client.flushdb()
    await redis_client.aclose()
    logger.info("Redis cache purged. Zero-downtime corpus deployment complete.")


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Pipeline de ingesta SeaweedFS S3 -> Qdrant.")
    parser.add_argument(
        "--reset", "--reiniciar", action="store_true",
        help="Reinicia y borra la carga anterior en PostgreSQL (atv.inventario_archivo) y limpia Qdrant antes de ingestar."
    )
    args, _ = parser.parse_known_args()

    if args.reset:
        logger.warning("Opción --reset activada: Eliminando carga anterior en PostgreSQL y limpiando Qdrant...")
        from scripts.reset_inventory import ejecutar_reinicio_completo
        await asyncio.to_thread(ejecutar_reinicio_completo, confirm=True, delete_db=True)
        logger.info("Reinicio completado. Continuando con la ingesta desde cero...")

    try:
        from scripts.sync_dag_airflow import asegurar_dag_en_airflow
        logger.info("Verificando presencia de dag_ingesta.py en Airflow (VPS2)...")
        await asyncio.to_thread(asegurar_dag_en_airflow)
    except Exception as exc:
        logger.warning(f"Omitiendo sincronización remota de DAG con VPS2: {exc}")

    await sincronizar_seaweedfs()
    await asyncio.to_thread(reconstruir_indice_qdrant)
    await promover_indice_y_purgar_cache(version_nueva=CURRENT_CORPUS_VERSION)

if __name__ == "__main__":
    asyncio.run(main())