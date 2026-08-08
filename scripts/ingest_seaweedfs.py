# scripts/ingest_seaweedfs.py
import asyncio
import logging
import aioboto3
from botocore.exceptions import ClientError
from sqlalchemy import select
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import CreateAliasOperation
from redis.asyncio import Redis

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.traceability import InventarioArchivo

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("atv.ingestion")


async def sincronizar_seaweedfs() -> None:
    """
    Phase 1: Network I/O Synchronization
    Lists objects in the SeaweedFS S3 bucket and compares ETags against the PostgreSQL
    inventory table to detect insertions, modifications, or deletions without downloading binaries.
    """
    logger.info(f"Connecting to SeaweedFS at {settings.s3_endpoint_url}...")
    session = aioboto3.Session()

    async with session.client('s3',
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region
    ) as s3:
        try:
            # Issue S3 ListObjectsV2 against the governed corpus bucket
            response = await s3.list_objects_v2(Bucket=settings.s3_bucket_corpus)
            remotos = response.get('Contents', [])
        except ClientError as exc:
            logger.error(f"Failed to read from SeaweedFS S3 bucket: {exc}")
            return

        async with AsyncSessionLocal() as db:
            # Materialize current PostgreSQL inventory into memory for O(1) lookups
            query = await db.execute(select(InventarioArchivo))
            inventario_local = {rec.nombre_archivo: rec for rec in query.scalars().all()}
            remotos_nombres = set()

            for obj in remotos:
                nombre = obj['Key']
                etag = obj['ETag'].strip('"')
                remotos_nombres.add(nombre)

                registro = inventario_local.get(nombre)

                if not registro:
                    logger.info(f"New technical sheet detected: {nombre}")
                    nuevo_reg = InventarioArchivo(
                        nombre_archivo=nombre,
                        etag_seaweedfs=etag,
                        version_corpus=0, # Will be updated by pdfplumber extraction phase
                        estado="NUEVO"
                    )
                    db.add(nuevo_reg)
                elif registro.etag_seaweedfs != etag:
                    logger.info(f"Modified technical sheet detected: {nombre}")
                    registro.etag_seaweedfs = etag
                    registro.estado = "MODIFICADO"

            # Detect deletions: Exists in DB but missing from SeaweedFS bucket
            for nombre, registro in inventario_local.items():
                if nombre not in remotos_nombres and registro.estado != "RETIRADO":
                    logger.warning(f"Technical sheet removed from SeaweedFS: {nombre}")
                    registro.estado = "RETIRADO"

            await db.commit()
            logger.info("SeaweedFS metadata synchronization complete.")


async def promover_indice_y_purgar_cache(version_nueva: int) -> None:
    """
    Phase 2: Atomic Alias Promotion & Cache Purging (Zero-Downtime Deployment)
    Swaps the production Qdrant alias to the newly built collection and clears
    the Redis semantic cache to enforce strict consistency and avoid obsolete agronomic answers.
    """
    alias = settings.qdrant_collection
    coleccion_nueva = f"{alias}_v{version_nueva}"

    logger.info(f"Promoting alias '{alias}' to vector collection '{coleccion_nueva}'...")
    qdrant = AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)

    try:
        # Atomic Alias Switch: Drops the old alias reference and points to the new collection instantly.
        await qdrant.update_collection_aliases(
            change_aliases_operations=[
                CreateAliasOperation(
                    collection_name=coleccion_nueva,
                    alias_name=alias
                )
            ]
        )
        logger.info(f"Qdrant alias successfully switched to {coleccion_nueva}.")
    except Exception as exc:
        logger.error(f"Failed to execute atomic Qdrant alias switch: {exc}")
        raise

    logger.info("Purging Redis Semantic Cache (DB 3)...")
    
    # Flush cache to prevent serving obsolete LLM generations based on previous vector states
    redis_client = Redis.from_url(settings.redis_url)
    await redis_client.flushdb()
    await redis_client.aclose()
    
    logger.info("Redis cache purged. Zero-downtime corpus deployment complete.")


async def main():
    """Airflow DAG Entrypoint"""
    await sincronizar_seaweedfs()
    
    # Example placeholder: the actual promotion runs ONLY after extraction/indexing finishes
    # await promover_indice_y_purgar_cache(version_nueva=2) 

if __name__ == "__main__":
    asyncio.run(main())