# app/models/telemetry.py
import uuid
from datetime import datetime, timezone
from typing import Optional, Any, Dict, List
from sqlalchemy import (
    String, Integer, Numeric, Boolean, Text, TIMESTAMP, 
    ForeignKey, Computed, Index
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase):
    """
    Base class for all SQLAlchemy 2.0 models.
    """
    pass

class ConsultaModel(Base):
    __tablename__ = "consulta"
    __table_args__ = (
        Index("idx_consulta_ts", "ts"),
        {"schema": "atv"}  # Strict schema isolation for Ley 787 compliance
    )

    consulta_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ts: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))
    vendedor_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    consulta_sanitizada: Mapped[str] = mapped_column(Text, nullable=False)
    intencion: Mapped[str] = mapped_column(String(32), nullable=False)
    confianza: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    escalada_hitl: Mapped[bool] = mapped_column(Boolean, default=False)
    respuesta: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    modelo: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    
    # Telemetry & Cost
    tokens_entrada: Mapped[int] = mapped_column(Integer, default=0)
    tokens_salida: Mapped[int] = mapped_column(Integer, default=0)
    costo_estimado_usd: Mapped[float] = mapped_column(Numeric(8, 6), default=0.0)
    
    # SLA APM Metrics (Milliseconds)
    lat_webhook_ms: Mapped[int] = mapped_column(Integer, default=0)
    lat_sanitizacion_ms: Mapped[int] = mapped_column(Integer, default=0)
    lat_clasif_ms: Mapped[int] = mapped_column(Integer, default=0)
    lat_retrieval_ms: Mapped[int] = mapped_column(Integer, default=0)
    lat_llm_ms: Mapped[int] = mapped_column(Integer, default=0)
    lat_total_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    
    # Database-generated computed column (Read-Only in Python)
    sla_cumplido: Mapped[bool] = mapped_column(
        Boolean, 
        Computed("lat_total_ms <= 10000", persisted=True)
    )
    
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    registros_usados: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(JSONB, nullable=True)
    version_corpus: Mapped[int] = mapped_column(Integer, nullable=False)


class ErrorLogModel(Base):
    __tablename__ = "error_log"
    __table_args__ = {"schema": "atv"}

    error_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ts: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))
    consulta_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("atv.consulta.consulta_id", ondelete="SET NULL"), nullable=True
    )
    componente: Mapped[str] = mapped_column(String(64), nullable=False)
    excepcion_tipo: Mapped[str] = mapped_column(String(128), nullable=False)
    mensaje: Mapped[str] = mapped_column(Text, nullable=False)
    stack_trace: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resuelto: Mapped[bool] = mapped_column(Boolean, default=False)