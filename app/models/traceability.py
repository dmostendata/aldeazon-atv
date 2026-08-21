# app/models/traceability.py
import uuid
from datetime import datetime, timezone
from typing import Optional, Any, Dict, List
from sqlalchemy import (
    Column, String, Integer, Numeric, Boolean, Text, TIMESTAMP, 
    ForeignKey, Computed, Index
)
from sqlalchemy.dialects.postgresql import UUID, JSONB, ARRAY, ENUM
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

intencion_enum = ENUM(
    'informativa', 'recomendacion', 'fuera_alcance',
    name='intencion_tipo',
    schema='atv',
    create_type=False
)

class Base(DeclarativeBase):
    """
    Root declarative base for all ATV SQLAlchemy 2.0 models.

    """
    pass


class Consulta(Base):
    """
    Runtime traceability log for online WhatsApp interactions.
    Enforces Ley 787 PII stripping and monitors the <=10s SLA.
    """
    __tablename__ = "consulta"
    __table_args__ = (
        Index("idx_consulta_ts", "ts"),
        Index("idx_consulta_intencion_hitl", "intencion", "escalada_hitl"),
        {"schema": "atv"}
    )

    consulta_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ts: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))
    vendedor_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    consulta_sanitizada: Mapped[str] = mapped_column(Text, nullable=False)
    intencion = Column(intencion_enum, nullable=False)
    confianza: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    escalada_hitl: Mapped[bool] = mapped_column(Boolean, default=False)
    respuesta: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    modelo: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    
    # Token Economics
    tokens_entrada: Mapped[int] = mapped_column(Integer, default=0)
    tokens_salida: Mapped[int] = mapped_column(Integer, default=0)
    costo_estimado_usd: Mapped[float] = mapped_column(Numeric(8, 6), default=0.0)
    
    # Micro-Latencies (SLA APM)
    lat_webhook_ms: Mapped[int] = mapped_column(Integer, default=0)
    lat_sanitizacion_ms: Mapped[int] = mapped_column(Integer, default=0)
    lat_clasif_ms: Mapped[int] = mapped_column(Integer, default=0)
    lat_retrieval_ms: Mapped[int] = mapped_column(Integer, default=0)
    lat_llm_ms: Mapped[int] = mapped_column(Integer, default=0)
    lat_total_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    
    # Database-computed SLA flag (read-only in ORM)[cite: 17]
    sla_cumplido: Mapped[bool] = mapped_column(
        Boolean, 
        Computed("lat_total_ms <= 10000", persisted=True)
    )
    
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    registros_usados: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(JSONB, nullable=True)
    version_corpus: Mapped[int] = mapped_column(Integer, nullable=False)

    # RSK-NEW-04 Commercial Sales Audit
    productos_sugeridos_ids: Mapped[Optional[List[int]]] = mapped_column(ARRAY(Integer), default=list)


class InventarioArchivo(Base):
    """
    SharePoint file inventory for offline Airflow DAG synchronization.
    Tracks ETags to prevent redundant downloads of unmodified PDFs (§6.5.1)[cite: 17].

    """
    __tablename__ = "inventario_archivo"
    __table_args__ = (
        Index("idx_inventario_nombre", "nombre_archivo", unique=True),
        Index("idx_inventario_estado", "estado"),
        {"schema": "atv"}
    )

    archivo_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    nombre_archivo: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    etag_sharepoint: Mapped[str] = mapped_column(String(128), nullable=False)
    version_corpus: Mapped[int] = mapped_column(Integer, nullable=False)
    
    # 'NUEVO', 'MODIFICADO', 'RETIRADO', 'INDEXADO', 'ERROR'
    estado: Mapped[str] = mapped_column(String(32), nullable=False)
    
    # 'tabla', 'columnas', 'prosa', 'vision'[cite: 17]
    metodo_extraccion: Mapped[str] = mapped_column(String(32), default="columnas")
    total_registros_atomicos: Mapped[int] = mapped_column(Integer, default=0)
    ts_cargado: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))
    ts_indexado: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class ErrorLog(Base):
    """
    System-wide exception log for Superset observability and DevOps alerting[cite: 17].
    """
    __tablename__ = "error_log"
    __table_args__ = (
        Index("idx_error_componente_ts", "componente", "ts"),
        {"schema": "atv"}
    )

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