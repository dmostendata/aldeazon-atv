# scripts/reset_inventory.py
"""
Forces a full re-ingestion pass after a change to extraction logic.

Unlike retry_failed.py (routine recovery for files stuck in the 'ERROR'
dead-letter state), this is a deliberate, destructive "rebuild the corpus"
operation. It resets EVERY already-indexed file back to a re-processable
state AND wipes the corresponding Qdrant collection, so stale/incorrect
embeddings (e.g. from a fixed extraction bug) don't linger alongside
freshly re-extracted points with new UUIDs.

Defaults to a dry run. Nothing is changed unless --confirm is passed.

Usage:
    python -m scripts.reset_inventory                     # dry run
    python -m scripts.reset_inventory --confirm            # reset DB + wipe Qdrant
    python -m scripts.reset_inventory --confirm --keep-qdrant  # DB reset only
"""
import argparse
import logging
from sqlalchemy import create_engine, update, func, select
from sqlalchemy.orm import sessionmaker
from qdrant_client import QdrantClient

from app.core.config import settings
from app.models.traceability import InventarioArchivo

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("atv.reset_inventory")

ALIAS_NAME = settings.qdrant_collection
CURRENT_CORPUS_VERSION = 1
COLLECTION_NAME = f"{ALIAS_NAME}_v{CURRENT_CORPUS_VERSION}"

# Both states represent files whose current Qdrant points may be stale or
# wrong under the *previous* extraction logic and need to be redone.
ESTADOS_A_REPROCESAR = ("INDEXADO", "ERROR")

engine = create_engine(settings.database_url)
Session = sessionmaker(bind=engine)


def reset_inventario(confirm: bool) -> int:
    db = Session()
    try:
        count = db.execute(
            select(func.count(InventarioArchivo.nombre_archivo))
            .where(InventarioArchivo.estado.in_(ESTADOS_A_REPROCESAR))
        ).scalar()

        if not confirm:
            logger.info(
                f"[DRY RUN] Would reset {count} file(s) currently in "
                f"{ESTADOS_A_REPROCESAR} to 'MODIFICADO'."
            )
            return count

        stmt = (
            update(InventarioArchivo)
            .where(InventarioArchivo.estado.in_(ESTADOS_A_REPROCESAR))
            .values(estado="MODIFICADO")
        )
        result = db.execute(stmt)
        db.commit()
        logger.info(
            f"Reset {result.rowcount} file(s) to 'MODIFICADO'. "
            f"They will be re-extracted on the next ingest run."
        )
        return result.rowcount
    finally:
        db.close()


def wipe_qdrant_collection(confirm: bool) -> None:
    qdrant = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)

    if not qdrant.collection_exists(COLLECTION_NAME):
        logger.info(f"Collection '{COLLECTION_NAME}' does not exist; nothing to wipe.")
        return

    count = qdrant.count(collection_name=COLLECTION_NAME).count

    if not confirm:
        logger.info(f"[DRY RUN] Would delete collection '{COLLECTION_NAME}' ({count} points).")
        return

    qdrant.delete_collection(COLLECTION_NAME)
    logger.warning(
        f"Deleted collection '{COLLECTION_NAME}' ({count} points removed). "
        f"It will be recreated fresh on the next ingest run."
    )


def main():
    parser = argparse.ArgumentParser(
        description="Force full re-ingestion: reset InventarioArchivo + wipe the Qdrant collection."
    )
    parser.add_argument(
        "--confirm", action="store_true",
        help="Actually perform the reset. Without this flag, only a dry-run summary is printed."
    )
    parser.add_argument(
        "--keep-qdrant", action="store_true",
        help="Only reset the DB inventory; leave the Qdrant collection untouched."
    )
    args = parser.parse_args()

    reset_inventario(args.confirm)

    if not args.keep_qdrant:
        wipe_qdrant_collection(args.confirm)
    else:
        logger.info(
            "Skipping Qdrant wipe (--keep-qdrant). Note: re-extracted records get new "
            "UUIDs and will coexist with old points unless you clean those up separately."
        )

    if not args.confirm:
        logger.info("This was a dry run. Re-run with --confirm to apply changes.")


if __name__ == "__main__":
    main()