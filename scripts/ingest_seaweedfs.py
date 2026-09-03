# scripts/ingest_seaweedfs.py
import os
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
from qdrant_client.models import CreateAliasOperation, CreateAlias
from redis.asyncio import Redis

from app.core.config import settings
from app.models.traceability import InventarioArchivo, ErrorLog
from app.services.extractor import extraer_desde_pdf

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("atv.ingestion")

STAGING_DIR = "/var/www/aldeazon-atv/fixtures/staging"
EMBEDDING_MODEL = "intfloat/multilingual-e5-large"
COLLECTION_NAME = "atv_corpus_v1"
ALIAS_NAME = settings.qdrant_collection
CURRENT_CORPUS_VERSION = 1

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


def reconstruir_indice_qdrant() -> None:
    lote_textos: List[str] = []
    lote_metadata: List[Dict[str, Any]] = []
    lote_ids: List[str] = []
    
    db = OfflineSyncSession()

    if not os.path.exists(STAGING_DIR):
        logger.error(f"Staging directory '{STAGING_DIR}' not found. Aborting index.")
        db.close()
        return

    archivos_staging = [f for f in sorted(os.listdir(STAGING_DIR)) if f.lower().endswith(".pdf")]
    logger.info(f"Found {len(archivos_staging)} PDF(s) in staging for extraction.")

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

        try:
            extracted_count = 0
            logger.info(f"Extracting atomic records from: {filename}...")
            
            for registro in extraer_desde_pdf(filepath, filename):
                lote_textos.append(registro.texto_para_embedding())
                lote_metadata.append(registro.model_dump())
                lote_ids.append(registro.registro_id)
                extracted_count += 1

            logger.info(f"Extracted {extracted_count} atomic tuples from '{filename}'.")

            if inv_entry:
                inv_entry.total_registros_atomicos = extracted_count
                inv_entry.estado = "INDEXADO" if extracted_count > 0 else "ERROR"
                inv_entry.ts_indexado = datetime.now(timezone.utc)
                db.commit()

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

    db.close()

    if not lote_ids:
        logger.error("[CRITICAL] Extraction finished with 0 chunks. Qdrant index will NOT be updated.")
        return

    logger.info(f"Indexing {len(lote_ids)} atomic tuples into Qdrant ({COLLECTION_NAME})...")
    qdrant = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    qdrant.set_model(EMBEDDING_MODEL)

    if qdrant.collection_exists(COLLECTION_NAME):
        qdrant.delete_collection(COLLECTION_NAME)

    qdrant.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=qdrant.get_fastembed_vector_params(),
    )

    qdrant.add(
        collection_name=COLLECTION_NAME,
        documents=lote_textos,
        metadata=lote_metadata,
        ids=lote_ids
    )
    logger.info(f"Vector indexing complete. {len(lote_ids)} points populated.")


async def promover_indice_y_purgar_cache(version_nueva: int) -> None:
    coleccion_nueva = f"{ALIAS_NAME}_v{version_nueva}"
    qdrant = AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)

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
    await sincronizar_seaweedfs()
    await asyncio.to_thread(reconstruir_indice_qdrant)
    await promover_indice_y_purgar_cache(version_nueva=CURRENT_CORPUS_VERSION)

if __name__ == "__main__":
    asyncio.run(main())