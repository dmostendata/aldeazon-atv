# scripts/inspect_qdrant.py
import asyncio
from qdrant_client import AsyncQdrantClient
from app.core.config import settings

async def inspect_vectors():
    client = AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    
    print(f"[*] Inspecting Alias: {settings.qdrant_collection}")
    
    try:
        # Fetch the total count of documents in the collection
        count_result = await client.count(collection_name=settings.qdrant_collection)
        print(f"[*] Total indexed records (RegistroAtomico chunks): {count_result.count}")
        
        if count_result.count == 0:
            print("[!] The collection is completely EMPTY. The PDF extractor failed to yield chunks.")
            return

        # Fetch a sample of 3 records to see what the LLM is actually reading
        records = await client.scroll(
            collection_name=settings.qdrant_collection,
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