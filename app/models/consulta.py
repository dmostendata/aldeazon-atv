from sqlalchemy import Column, String, Integer, Numeric, Boolean, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB, TIMESTAMP
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql import func
import uuid

Base = declarative_base()

class Consulta(Base):
    """
    SQLAlchemy model for the runtime traceability table[cite: 1].
    This acts as the append-only event log required for Ley 787 compliance[cite: 1].
    """
    __tablename__ = "consulta"

    # This instructs the SQLAlchemy Metadata registry to strictly scope DDL and DML to the 'atv' schema.
    __table_args__ = {"schema": "atv"}

    # UUID generation is handled by the Python runtime for the primary key
    consulta_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # server_default=func.now() delegates the timestamp creation to the PostgreSQL engine
    ts = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    
    vendedor = Column(String, nullable=False, index=True)
    consulta = Column(Text, nullable=False)
    intencion = Column(String, index=True)
    confianza = Column(Numeric(4, 3))
    texto_sanitizado = Column(Text, nullable=False)
    
    # Boolean for tracking Human-In-The-Loop escalation[cite: 1]
    escalada_hitl = Column(Boolean, nullable=False, default=False, index=True)
    respuesta = Column(Text)
    modelo = Column(String)
    
    # Telemetry metrics required for the Superset dashboard[cite: 1]
    tokens_entrada = Column(Integer)
    tokens_salida = Column(Integer)
    lat_webhook_ms = Column(Integer)
    lat_clasif_ms = Column(Integer)
    lat_retrieval_ms = Column(Integer)
    lat_llm_ms = Column(Integer)
    lat_total_ms = Column(Integer)
    
    # JSONB is native to Postgres and maps directly to Python dictionaries
    registros_usados = Column(JSONB)
    version_corpus = Column(Integer)