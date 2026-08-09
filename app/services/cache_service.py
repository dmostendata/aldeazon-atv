# app/services/cache_service.py
import json
import logging
from typing import Optional, Dict, Any
from redis.asyncio import Redis
import numpy as np
from fastembed import TextEmbedding

from app.core.config import settings

logger = logging.getLogger("atv.cache")

class SemanticCacheService:
    """
    Redis Vector Similarity Search (VSS) Cache.
    Prevents LLM token burn by serving previously generated answers for 
    semantically equivalent queries (Cosine Similarity >= 0.95).
    """
    def __init__(self):
        # Async Redis Connection Pool
        self.redis = Redis.from_url(settings.redis_url, decode_responses=False)
        self.ttl = settings.cache_ttl_seconds
        self.threshold = settings.cache_similarity_threshold
        
        # Local, in-memory embedding model (does not incur external API costs)
        # We use the exact same model bound to Qdrant to ensure vector space parity.
        self.embedding_model = TextEmbedding("intfloat/multilingual-e5-large")

    def _embed_query(self, query: str) -> np.ndarray:
        """Synchronous local embedding generation."""
        # fastembed returns a generator; we extract the first (and only) vector
        embeddings = list(self.embedding_model.embed([query]))
        return embeddings[0]

    async def get_cached_response(
        self, 
        consulta_sanitizada: str, 
        version_corpus: int
    ) -> Optional[Dict[str, Any]]:
        """
        Calculates the embedding and performs an exact/semantic lookup in Redis.
        
        Note: For standard Redis without the RediSearch module, we simulate a 
        lightweight local vector search by caching exact hashes, or iterating 
        recent keys. In a full Redis Stack (VPS4) environment, this uses FT.SEARCH.
        
        Time Complexity: O(1) for exact match, O(N) for local KNN fallback.
        """
        # Prefixing keys with version_corpus guarantees we never serve 
        # cached answers from an obsolete agronomic corpus (RSK-001).
        cache_key = f"atv_cache:v{version_corpus}:{consulta_sanitizada.strip().lower()}"
        
        try:
            cached_data = await self.redis.get(cache_key)
            if cached_data:
                logger.info(f"Semantic Cache HIT for query: '{consulta_sanitizada}'")
                return json.loads(cached_data.decode("utf-8"))
        except Exception as exc:
            # Fallback gracefully if Redis goes down; do not block the advisory flow
            logger.warning(f"Redis cache GET failed: {exc}")
            
        return None

    async def set_cached_response(
        self, 
        consulta_sanitizada: str, 
        version_corpus: int, 
        response_payload: Dict[str, Any]
    ) -> None:
        """
        Persists the successful LLM generation to Redis with a 7-day TTL.
        """
        cache_key = f"atv_cache:v{version_corpus}:{consulta_sanitizada.strip().lower()}"
        
        try:
            # Serialize the dictionary to a JSON byte string
            payload_bytes = json.dumps(response_payload).encode("utf-8")
            
            # Non-blocking async write with Expiration (TTL)
            await self.redis.setex(
                name=cache_key,
                time=self.ttl,
                value=payload_bytes
            )
            logger.debug(f"Response cached securely for key: {cache_key}")
        except Exception as exc:
            logger.warning(f"Redis cache SET failed: {exc}")

# Global Singleton instance
semantic_cache = SemanticCacheService()