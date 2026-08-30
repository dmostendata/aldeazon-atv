# scripts/ingest_seaweedfs.py
import os
import asyncio
import logging
import aioboto3
from botocore.exceptions import ClientError
from botocore.config import Config
from datetime import datetime, timezone
from typing import List, Dict, Any

from sqlalchemy import select
from qdrant_client import AsyncQdrantClient, QdrantClient
from qdrant_client.models import CreateAliasOperation, CreateAlias
from redis.asyncio import Redis

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.core.database import SessionLocal
from app.models.traceability import InventarioArchivo, ErrorLog
from app.services.extractor import extraer_desde_pdf

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("atv.ingestion")

STAGING_DIR = "fixtures/staging"
EMBEDDING_MODEL = "intfloat/multilingual-e5-large"
COLLECTION_NAME = "atv_corpus_v1"
ALIAS_NAME = settings.qdrant_collection
CURRENT_CORPUS_VERSION = 1

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
            response = await s3.list_objects_v2(Bucket=settings.s3_bucket_corpus, Prefix="raw/ostendata/")
            remotos = response.get('Contents', [])
        except ClientError as exc:
            logger.error(f"S3 Connection Failed: {exc}")
            return

        async with AsyncSessionLocal() as db:
            query = await db.execute(select(InventarioArchivo))
            inventario_local = {rec.nombre_archivo: rec for rec in query.scalars().all()}
            remotos_nombres = set()

            for obj in remotos:
                nombre = obj['Key']
                
                # FIX: Strict Extension & Directory Filter
                # Prevents passing .mp4 or .m4a binaries to pdfplumber
                if nombre.endswith('/') or not nombre.lower().endswith('.pdf'):
                    continue

                etag = obj['ETag'].strip('"')
                remotos_nombres.add(nombre)
                registro = inventario_local.get(nombre)
                descargar = False

                if not registro:
                    logger.info(f"New PDF detected: {nombre}")
                    nuevo_reg = InventarioArchivo(
                        nombre_archivo=nombre,
                        etag_seaweedfs=etag,
                        version_corpus=CURRENT_CORPUS_VERSION,
                        estado="NUEVO"
                    )
                    db.add(nuevo_reg)
                    descargar = True
                elif registro.etag_seaweedfs != etag:
                    logger.info(f"Modified PDF detected: {nombre}")
                    registro.etag_seaweedfs = etag
                    registro.estado = "MODIFICADO"
                    descargar = True

                if descargar:
                    filename_local = os.path.basename(nombre)
                    filepath = os.path.join(STAGING_DIR, filename_local)
                    try:
                        logger.info(f"Downloading stream for {filename_local}...")
                        await s3.download_file(settings.s3_bucket_corpus, nombre, filepath)
                    except Exception as e:
                        logger.error(f"I/O error downloading '{nombre}': {e}")
                        if registro: registro.estado = "ERROR"

            # Detect deletions (Soft-delete state transition)
            for nombre, registro in inventario_local.items():
                if nombre not in remotos_nombres and registro.estado != "RETIRADO":
                    registro.estado = "RETIRADO"

            await db.commit()
            logger.info("SeaweedFS sync & binary downloads complete.")

def reconstruir_indice_qdrant() -> None:
    """
    Phase 2: Extraction & Vector Indexing.
    Architectural Parallel: A synchronous Laravel Queue Job handling heavy CPU-bound processing.
    """
    lote_textos, lote_metadata, lote_ids = [], [], []
    db = SessionLocal()
    
    for filename in sorted(os.listdir(STAGING_DIR)):
        if not filename.lower().endswith(".pdf"):
            continue
        
        filepath = os.path.join(STAGING_DIR, filename)
        s3_key = f"raw/ostendata/aldeazon-atv/Fichas Técnicas De Productos/{filename}"
        
        inv_entry = db.execute(select(InventarioArchivo).where(InventarioArchivo.nombre_archivo == s3_key)).scalar_one_or_none()

        if inv_entry and inv_entry.estado == "ERROR":
            continue

        try:
            extracted_count = 0
            for registro in extraer_desde_pdf(filepath, filename):
                lote_textos.append(registro.texto_para_embedding())
                lote_metadata.append(registro.model_dump())
                lote_ids.append(registro.registro_id)
                extracted_count += 1
            
            if inv_entry:
                inv_entry.total_registros_atomicos = extracted_count
                inv_entry.estado = "INDEXADO"
                inv_entry.ts_indexado = datetime.now(timezone.utc)
                db.commit()
        except Exception as e:
            logger.error(f"PDF Extraction failed for '{filename}': {e}")
            db.rollback()

    if not lote_ids:
        return

    logger.info(f"Loading {EMBEDDING_MODEL} into RAM for indexing...")
    qdrant = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    qdrant.set_model(EMBEDDING_MODEL)

    if qdrant.collection_exists(COLLECTION_NAME):
        qdrant.delete_collection(COLLECTION_NAME)

    qdrant.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=qdrant.get_fastembed_vector_params(),
    )

    qdrant.add(collection_name=COLLECTION_NAME, documents=lote_textos, metadata=lote_metadata, ids=lote_ids)
    logger.info("Extraction and Indexing complete.")

async def promover_indice_y_purgar_cache(version_nueva: int) -> None:
    # Phase 3: Zero-Downtime Deployment
    coleccion_nueva = f"{ALIAS_NAME}_v{version_nueva}"
    qdrant = AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)

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