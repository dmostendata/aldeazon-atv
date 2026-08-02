# scripts/ingest_sharepoint.py
import os
import logging
import traceback
import warnings
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime, timezone
from typing import Iterator, Dict, Any, List
from msal import ConfidentialClientApplication
from qdrant_client import QdrantClient
from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.traceability import InventarioArchivo, ErrorLog
from app.services.extractor import extraer_desde_pdf

warnings.filterwarnings("ignore", category=UserWarning, module="qdrant_client")
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

STAGING_DIR = "fixtures/staging"
EMBEDDING_MODEL = "intfloat/multilingual-e5-large"
COLLECTION_NAME = "atv_corpus_v1"
ALIAS_NAME = "atv_corpus"
CURRENT_CORPUS_VERSION = 1


def get_resilient_http_session() -> requests.Session:
    """
    Configures an HTTP session with exponential backoff for transient TCP/Graph errors.
    """
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=True
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def get_graph_token() -> str:
    """Acquires Microsoft Entra ID Client Credentials token[cite: 10]."""
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
    """Recursively yields PDF file metadata from SharePoint folders[cite: 10]."""
    url = (
        f"https://graph.microsoft.com/v1.0/sites/{settings.ms_site_id}/"
        f"drives/{settings.ms_drive_id}/items/{item_id}/children"
    )
    headers = {"Authorization": f"Bearer {access_token}"}
    session = get_resilient_http_session()

    while url:
        response = session.get(url, headers=headers, timeout=15)
        data = response.json()

        for item in data.get("value", []):
            if "folder" in item:
                yield from fetch_pdfs_recursively(access_token, item["id"])
            elif "file" in item and item["name"].lower().endswith(".pdf"):
                yield item

        url = data.get("@odata.nextLink")


def download_file(session: requests.Session, download_url: str, filepath: str) -> None:
    """Downloads binary file streams in 8 KB chunks with automatic retries."""
    with session.get(download_url, stream=True, timeout=30) as r:
        with open(filepath, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)


def log_ingestion_error(db, filename: str, exc: Exception) -> None:
    """Persists unhandled exceptions into atv.error_log for Superset observability[cite: 10]."""
    err = ErrorLog(
        componente="airflow_ingesta",
        excepcion_tipo=type(exc).__name__,
        mensaje=f"Fallo procesando archivo {filename}: {str(exc)}",
        stack_trace="".join(traceback.format_tb(exc.__traceback__)),
        resuelto=False
    )
    db.add(err)
    db.commit()


def sincronizar_sharepoint(db, http_session: requests.Session, token: str) -> None:
    """
    Phase 1: Network I/O Synchronization (ODAI Blueprint §6.5.1)[cite: 10].
    Downloads only new or modified PDFs based on ETag comparison against atv.inventario_archivo.
    """
    logger.info("Checking SharePoint ETag inventory...")
    for item in fetch_pdfs_recursively(token):
        filename = item["name"]
        etag = item.get("eTag", "")
        download_url = item.get("@microsoft.graph.downloadUrl")
        if not download_url:
            continue

        # Natural key is the filename, not the ETag: SharePoint mints a new
        # ETag every time a file is edited, so keying the lookup on etag
        # caused a fresh row to be inserted per edit instead of updating
        # the existing inventory record.
        existing_record = db.execute(
            select(InventarioArchivo).where(InventarioArchivo.nombre_archivo == filename)
        ).scalar_one_or_none()

        if existing_record and existing_record.etag_sharepoint == etag and existing_record.estado == "INDEXADO":
            logger.info(f"Skipping download (ETag match): {filename}")
            continue

        filepath = os.path.join(STAGING_DIR, filename)

        if not existing_record:
            inv_entry = InventarioArchivo(
                nombre_archivo=filename,
                etag_sharepoint=etag,
                version_corpus=CURRENT_CORPUS_VERSION,
                estado="NUEVO"
            )
            db.add(inv_entry)
        else:
            inv_entry = existing_record
            inv_entry.etag_sharepoint = etag
            inv_entry.estado = "MODIFICADO"

        db.commit()
        db.refresh(inv_entry)

        try:
            logger.info(f"Downloading remote file: {filename}")
            download_file(http_session, download_url, filepath)
        except Exception as e:
            logger.error(f"Network error downloading '{filename}': {str(e)}")
            db.rollback()
            inv_entry.estado = "ERROR"
            db.add(inv_entry)
            db.commit()
            log_ingestion_error(db, filename, e)


def reconstruir_indice_qdrant(db) -> None:
    """
    Phase 2: Complete Corpus Extraction & Atomic Indexing (ODAI Blueprint §6.5.7)[cite: 10].
    Scans local staging disk and rebuilds the Qdrant vector space from all valid PDFs.
    """
    lote_textos: List[str] = []
    lote_metadata: List[Dict[str, Any]] = []
    lote_ids: List[str] = []

    logger.info("Starting complete corpus extraction from local staging...")
    if not os.path.exists(STAGING_DIR):
        logger.warning(f"Staging directory '{STAGING_DIR}' does not exist.")
        return

    for filename in sorted(os.listdir(STAGING_DIR)):
        if not filename.lower().endswith(".pdf"):
            continue
        
        filepath = os.path.join(STAGING_DIR, filename)
        
        # Look up inventory state to skip permanently corrupted files
        inv_entry = db.execute(
            select(InventarioArchivo).where(InventarioArchivo.nombre_archivo == filename)
        ).scalar_one_or_none()

        if inv_entry and inv_entry.estado == "ERROR":
            logger.warning(f"Skipping extraction for known corrupt file: {filename}")
            continue

        extracted_count = 0
        try:
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
            logger.error(f"Error extracting from '{filename}': {type(e).__name__} - {str(e)}")
            db.rollback()
            if inv_entry:
                inv_entry.estado = "ERROR"
                db.add(inv_entry)
                db.commit()
            log_ingestion_error(db, filename, e)

    if not lote_ids:
        logger.warning("No valid agricultural records extracted from staging. Aborting index rebuild.")
        return

    logger.info(f"Configuring local Qdrant model: {EMBEDDING_MODEL}...")
    qdrant = QdrantClient(url="http://localhost:6333")
    qdrant.set_model(EMBEDDING_MODEL)

    if qdrant.collection_exists(COLLECTION_NAME):
        qdrant.delete_collection(COLLECTION_NAME)

    # Dynamic FastEmbed schema generation matching named vector parameters
    qdrant.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=qdrant.get_fastembed_vector_params(),
    )

    logger.info(f"Upserting {len(lote_ids)} vectors into Qdrant...")
    qdrant.add(
        collection_name=COLLECTION_NAME,
        documents=lote_textos,
        metadata=lote_metadata,
        ids=lote_ids,
    )

    # Atomic Alias Switch (ODAI Blueprint §6.4.3)[cite: 10]
    qdrant.update_collection_aliases(
        change_aliases_operations=[
            {"create_alias": {"collection_name": COLLECTION_NAME, "alias_name": ALIAS_NAME}}
        ]
    )
    logger.info(f"Indexing complete. Alias '{ALIAS_NAME}' active.")


def main() -> None:
    os.makedirs(STAGING_DIR, exist_ok=True)
    logger.info("Authenticating with Microsoft Graph...")
    token = get_graph_token()

    db = SessionLocal()
    http_session = get_resilient_http_session()

    try:
        sincronizar_sharepoint(db, http_session, token)
        reconstruir_indice_qdrant(db)
    finally:
        http_session.close()
        db.close()


if __name__ == "__main__":
    main()