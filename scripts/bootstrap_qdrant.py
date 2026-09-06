import asyncio
import logging
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, VectorParams
from qdrant_client.http.models import models
from app.core.config import settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("atv.bootstrap")

async def bootstrap_vector_db():
    client = AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    base_collection = f"{settings.qdrant_collection}_v1"

    try:
        # Enforce idempotency: Verify existence before creating
        if not await client.collection_exists(collection_name=base_collection):
            logger.info(f"Creating collection: {base_collection} (1024-dim)...")
            await client.create_collection(
                collection_name=base_collection,
                vectors_config={"dense": VectorParams(size=1024, distance=Distance.COSINE)}
            )
        else:
            logger.info(f"Collection {base_collection} already exists. Bypassing creation.")

        # The script will now reach this point safely and attach the alias
        logger.info(f"Attaching alias: {settings.qdrant_collection} -> {base_collection}")
        await client.update_collection_aliases(change_aliases_operations=[
            models.CreateAliasOperation(
                create_alias=models.CreateAlias(
                    collection_name=base_collection,
                    alias_name=settings.qdrant_collection
                )
            )
        ])
        logger.info("Qdrant bootstrap complete. Ready for FastAPI serving.")
        
    except Exception as exc:
        logger.error(f"Bootstrap failed: {exc}")

if __name__ == "__main__":
    asyncio.run(bootstrap_vector_db())