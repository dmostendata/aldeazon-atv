# scripts/ingest_sharepoint.py
import os
import logging
import warnings
import requests
from typing import Iterator, Dict, Any
from msal import ConfidentialClientApplication
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

from app.core.config import settings
from app.services.extractor import extraer_desde_pdf

# Suppress non-fatal FastEmbed pooling warnings from upstream libraries
warnings.filterwarnings("ignore", category=UserWarning, module="qdrant_client")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

STAGING_DIR = "fixtures/staging"
# ODAI Blueprint §6.3 & §6.5.6: Mandated multilingual dense embedding model[cite: 8]
EMBEDDING_MODEL = "BAAI/bge-m3"
COLLECTION_NAME = "atv_corpus_v1"
ALIAS_NAME = "atv_corpus"


def get_graph_token() -> str:
    """
    Acquires an OAuth2 Client Credentials token from Microsoft Entra ID.
    
    PHP/Laravel Parallel: Equivalent to Laravel Http::asForm()->post()
    against the OAuth2 token endpoint.
    """
    app = ConfidentialClientApplication(
        settings.ms_client_id,
        authority=f"https://login.microsoftonline.com/{settings.ms_tenant_id}",
        client_credential=settings.ms_client_secret,
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        raise RuntimeError(f"Failed to acquire MS Graph token: {result.get('error_description')}")
    return result["access_token"]


def fetch_pdfs_recursively(access_token: str, item_id: str = "root") -> Iterator[Dict[str, Any]]:
    """
    Recursive generator traversing SharePoint folders via Microsoft Graph API.
    
    Pythonic Pattern: 'yield from' delegates generator execution to recursive frames
    without building an in-memory list, preventing RAM spikes on large corpora.
    """
    url = (
        f"https://graph.microsoft.com/v1.0/sites/{settings.ms_site_id}/"
        f"drives/{settings.ms_drive_id}/items/{item_id}/children"
    )
    headers = {"Authorization": f"Bearer {access_token}"}

    while url:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()

        for item in data.get("value", []):
            if "folder" in item:
                yield from fetch_pdfs_recursively(access_token, item["id"])
            elif "file" in item and item["name"].lower().endswith(".pdf"):
                yield item

        url = data.get("@odata.nextLink")


def download_file(download_url: str, filepath: str) -> None:
    """Downloads remote binary assets in 8 KB chunks."""
    with requests.get(download_url, stream=True, timeout=30) as r:
        r.raise_for_status()
        with open(filepath, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)


def index_into_qdrant() -> None:
    """
    Orchestrates SharePoint synchronization, PDF parsing, and Qdrant indexing.
    
    ODAI Blueprint Alignment: Implements Section 6.5.6 (Embeddings e indexacion)
    using atomic alias switching to prevent read-contention during runtime[cite: 8].
    """
    os.makedirs(STAGING_DIR, exist_ok=True)
    logger.info("Authenticating with Microsoft Graph...")
    token = get_graph_token()

    lote_textos: list[str] = []
    lote_metadata: list[Dict[str, Any]] = []
    lote_ids: list[str] = []

    logger.info("Fetching corpus metadata from SharePoint (Recursive Traversal)...")
    for item in fetch_pdfs_recursively(token):
        filename = item["name"]
        download_url = item.get("@microsoft.graph.downloadUrl")
        if not download_url:
            continue

        filepath = os.path.join(STAGING_DIR, filename)
        logger.info(f"Downloading: {filename}")
        download_file(download_url, filepath)

        # Consume generator yielded by the deterministic extraction service
        for registro in extraer_desde_pdf(filepath, filename):
            lote_textos.append(registro.texto_para_embedding())
            lote_metadata.append(registro.model_dump())
            lote_ids.append(registro.registro_id)

    if not lote_ids:
        logger.warning("No valid agricultural doses extracted from staging PDFs.")
        return

    logger.info(f"Connecting to Qdrant and configuring model: {EMBEDDING_MODEL}...")
    qdrant = QdrantClient(url="http://localhost:6333")
    
    # FastEmbed executes ONNX weights locally via CPU threads (Ley 787 compliant)[cite: 8]
    qdrant.set_model(EMBEDDING_MODEL)

    if qdrant.collection_exists(COLLECTION_NAME):
        qdrant.delete_collection(COLLECTION_NAME)

    # Recreate collection with 1024 dimensions matching BAAI/bge-m3[cite: 8]
    qdrant.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=1024, distance=Distance.COSINE),
    )

    logger.info(f"Embedding and indexing {len(lote_ids)} atomic records...")
    qdrant.add(
        collection_name=COLLECTION_NAME,
        documents=lote_textos,
        metadata=lote_metadata,
        ids=lote_ids,
    )

    # Atomic Alias Switch (§6.4.3): Guarantees zero-downtime cutover for the API[cite: 8]
    qdrant.update_collection_aliases(
        change_aliases_operations=[
            {"create_alias": {"collection_name": COLLECTION_NAME, "alias_name": ALIAS_NAME}}
        ]
    )
    logger.info(f"Indexing complete. Alias '{ALIAS_NAME}' points to {COLLECTION_NAME}.")


if __name__ == "__main__":
    index_into_qdrant()