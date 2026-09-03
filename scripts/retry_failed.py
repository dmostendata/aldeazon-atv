# scripts/retry_failed.py
from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker
from app.core.config import settings
from app.models.traceability import InventarioArchivo

# Stateless connection to reset the dead-letter queue
engine = create_engine(settings.database_url)
Session = sessionmaker(bind=engine)

def retry_failed():
    db = Session()
    # Reset all stuck 'ERROR' states back to 'NUEVO'
    stmt = update(InventarioArchivo).where(InventarioArchivo.estado == 'ERROR').values(estado='NUEVO')
    result = db.execute(stmt)
    db.commit()
    print(f"[*] MLOps: {result.rowcount} dead-letter files reset to 'NUEVO'. Ready for ingestion.")
    db.close()

if __name__ == "__main__":
    retry_failed()