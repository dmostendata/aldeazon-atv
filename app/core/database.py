# app/core/database.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.config import settings

# Engine manages the connection pool to VPS3. 
engine = create_engine(settings.database_url, pool_pre_ping=True)

# SessionLocal is the factory for new DB sessions.
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    """
    Dependency Injection provider for the DB session.
    In FastAPI, the 'yield' keyword turns this into a Context Manager.
    It opens the connection, hands it to the Controller, and the 'finally' 
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()