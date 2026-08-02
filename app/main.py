# app/main.py
import time
import logging
from fastapi import FastAPI, Depends, HTTPException, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session
from qdrant_client import QdrantClient

from app.core.config import settings
from app.core.database import engine, get_db
from app.models.traceability import Base, Consulta, ErrorLog
from app.services.rag_service import ATVRagService

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

with engine.connect() as conn:
    conn.execute(text("CREATE SCHEMA IF NOT EXISTS atv"))
    conn.commit()

Base.metadata.create_all(bind=engine)

app = FastAPI(title="ATV Internal API - AldeaZON")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)
qdrant_client = QdrantClient(url=settings.qdrant_url)


async def verify_api_key(api_key: str = Security(api_key_header)):
    VALID_API_KEY = settings.api_key
    if api_key != VALID_API_KEY:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    return api_key


class ChatRequest(BaseModel):
    vendedor_id: str = Field(..., description="Hash/ID del vendedor en tienda")
    consulta: str = Field(..., min_length=2, max_length=1000)
    sesion_id: str | None = Field(None, description="Idempotencia/sesión")


class ChatResponse(BaseModel):
    respuesta: str
    fuente: str | None
    escalada_hitl: bool
    latencia_ms: int


@app.post("/api/v1/consultar", response_model=ChatResponse, dependencies=[Depends(verify_api_key)])
async def procesar_consulta_interna(
    request: ChatRequest, 
    db: Session = Depends(get_db)
):
    """
    Online RAG Endpoint for WhatsApp Business Cloud API integration.
    
    PHP/Laravel Parallel: Operates as an Invokable Controller method
    ($request, DB::transaction) handling SLA auditing, error persistence, and HTTP formatting.
    """
    start_time = time.perf_counter()
    
    rag_service = ATVRagService(qdrant_client=qdrant_client)
    result = await rag_service.procesar_consulta(request.consulta)
    
    lat_total = int((time.perf_counter() - start_time) * 1000)
    
    # Enforce ODAI Blueprint §6.7 SLA budget threshold (10,000 ms)[cite: 14]
    if lat_total > 10000:
        logger.warning(f"SLA Breach detected: Total latency {lat_total}ms exceeds 10,000ms threshold.")
        err_sla = ErrorLog(
            componente="atv_api",
            excepcion_tipo="SLABreachWarning",
            mensaje=f"Presupuesto de latencia excedido: {lat_total}ms para vendedor {request.vendedor_id}",
            resuelto=False
        )
        db.add(err_sla)

    # Persist runtime exceptions to atv.error_log[cite: 14]
    # PHP/Laravel Parallel: Similar to triggering report($e) when a domain failure occurs.
    if result.error_tipo:
        logger.warning(f"Runtime failure trapped in [{result.componente_fallo}]: {result.error_tipo}")
        err_runtime = ErrorLog(
            componente=result.componente_fallo or "rag_service",
            excepcion_tipo=result.error_tipo,
            mensaje=result.error_mensaje or "Error no especificado en tiempo de ejecución",
            stack_trace=result.error_stack,
            resuelto=False
        )
        db.add(err_runtime)

    costo_usd = (result.tokens_entrada * 0.00000015) + (result.tokens_salida * 0.00000060)

    # Persist runtime event log to PostgreSQL for Apache Superset auditing (§6.4.4)[cite: 14]
    log_registro = Consulta(
        vendedor_hash=request.vendedor_id,
        consulta_sanitizada=request.consulta,
        intencion=result.intencion,
        confianza=result.confianza,
        escalada_hitl=result.escalada_hitl,
        respuesta=result.respuesta,
        modelo=result.modelo_utilizado,
        tokens_entrada=result.tokens_entrada,
        tokens_salida=result.tokens_salida,
        costo_estimado_usd=costo_usd,
        lat_webhook_ms=0,
        lat_sanitizacion_ms=result.lat_sanitizacion_ms,
        lat_retrieval_ms=result.lat_retrieval_ms,
        lat_llm_ms=result.lat_llm_ms,
        lat_total_ms=lat_total,
        registros_usados=result.registros_usados,
        version_corpus=1
    )
    
    db.add(log_registro)
    
    # Single atomic commit writes both atv.consulta and atv.error_log
    db.commit()
    db.refresh(log_registro)
    
    return ChatResponse(
        respuesta=result.respuesta,
        fuente=result.fuente,
        escalada_hitl=result.escalada_hitl,
        latencia_ms=lat_total
    )