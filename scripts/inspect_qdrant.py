# scripts/inspect_qdrant.py
import asyncio
import sys
from qdrant_client import AsyncQdrantClient
from app.core.config import settings

async def inspect_vectors():
    client = AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)

    # Allow inspecting a raw collection name (e.g. "atv_corpus_v1") as a
    # fallback for when the alias hasn't been promoted yet:
    #   python -m scripts.inspect_qdrant atv_corpus_v1
    target = sys.argv[1] if len(sys.argv) > 1 else settings.qdrant_collection
    print(f"[*] Inspecting: {target}")

    if not await client.collection_exists(target):
        print(f"[!] '{target}' does not exist as a collection or alias.")
        print("[*] Collections that DO exist on this Qdrant instance:")
        for c in (await client.get_collections()).collections:
            print(f"    - {c.name}")
        return

    try:
        # Fetch the total count of documents in the collection
        count_result = await client.count(collection_name=target)
        print(f"[*] Total indexed records (RegistroAtomico chunks): {count_result.count}")
        
        if count_result.count == 0:
            print("[!] The collection is completely EMPTY. The PDF extractor failed to yield chunks.")
            return

        # Fetch a sample of 3 records to see what the LLM is actually reading
        records = await client.scroll(
            collection_name=target,
            limit=3,
            with_payload=True,
            with_vectors=False
        )
        
        print("\n[*] Sample Payloads:")
        for point in records[0]:
            print(f" - ID: {point.id}")
            print(f"   Source: {point.payload.get('documento_fuente')}")
            print(f"   Product: {point.payload.get('producto')}")
            print(f"   Crop: {point.payload.get('cultivo')}")
            print(f"   Dose: {point.payload.get('dosis')}")
            print("-" * 40)
            
    except Exception as e:
        print(f"[!] Qdrant connection/query error: {e}")

if __name__ == "__main__":
    asyncio.run(inspect_vectors())