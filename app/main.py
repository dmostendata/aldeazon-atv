# app/main.py
import time
import logging
from fastapi import FastAPI, Depends, HTTPException, Security
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession
from qdrant_client import AsyncQdrantClient

# Core and Database
from app.core.config import settings
from app.db.session import get_db_session
from app.models.traceability import Consulta, ErrorLog

# Services and Schemas
from app.services.rag_service import ATVRagService
from app.schemas.domain import ChatResponse
from pydantic import BaseModel, Field

from fastembed import TextEmbedding

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("atv.gateway")

# Schema generation is delegated to raw SQL / Alembic, isolating DDL from the runtime.
app = FastAPI(title="ATV Internal API - AldeaZON")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)

# Async Qdrant Client instantiated globally (Singleton)
qdrant_client = AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)


async def verify_api_key(api_key: str = Security(api_key_header)):
    """Validates the static VPC token."""
    if api_key != settings.api_key:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    return api_key


class ChatRequest(BaseModel):
    """Incoming JSON payload from App AldeaZON."""
    vendedor_id: str = Field(..., description="Hash/ID del vendedor en tienda")
    sucursal_id: int = Field(..., description="ID de la sucursal de AldeaZON (stc2.sucursal)")
    consulta: str = Field(..., min_length=2, max_length=1000)
    sesion_id: str | None = Field(None, description="Idempotencia/sesión")

logger.info("Pre-loading bge-m3 dense embedding model into RAM (Singleton)...")
embedding_model_singleton = TextEmbedding("intfloat/multilingual-e5-large")

@app.post("/api/v1/consultar", response_model=ChatResponse, dependencies=[Depends(verify_api_key)])
async def procesar_consulta_interna(
    request: ChatRequest, 
    db: AsyncSession = Depends(get_db_session)
):
    """
    Synchronous REST endpoint executing the RAG pipeline and Commercial Match.
    Strictly enforced to complete within the 10,000ms SLA.
    """
    start_time = time.perf_counter()
    
    # 1. Inject connections into the RAG Orchestrator
    rag_service = ATVRagService(
        qdrant_client=qdrant_client, 
        embedding_model=embedding_model_singleton,
        db_session=db
    )
    result = await rag_service.procesar_consulta(request.consulta, request.sucursal_id)
    
    lat_total = int((time.perf_counter() - start_time) * 1000)
    
    # 2. SLA Budget Enforcement Logging
    if lat_total > settings.sla_latency_threshold_ms:
        logger.warning(f"SLA Breach detected: Total latency {lat_total}ms.")

    costo_usd = (result.tokens_entrada * 0.00000015) + (result.tokens_salida * 0.00000060)
    productos_sugeridos_ids = [item.sku_id for item in result.carrito] if result.carrito else []

    # 3. Asynchronous Traceability Persistence (Ley 787 & Token Economics)
    log_registro = Consulta(
        vendedor_hash=request.vendedor_id,
        consulta_sanitizada=result.consulta_sanitizada,  # Storing scrubbed text only
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
        lat_clasif_ms=result.lat_clasif_ms,
        lat_retrieval_ms=result.lat_retrieval_ms,
        lat_llm_ms=result.lat_llm_ms,
        lat_total_ms=lat_total,
        cache_hit=False,
        registros_usados=result.registros_usados,
        version_corpus=1,
        productos_sugeridos_ids=productos_sugeridos_ids
    )
    db.add(log_registro)
    
    # 4. Global Error Trap Persistence
    if result.error_tipo:
        err_runtime = ErrorLog(
            componente=result.componente_fallo or "rag_service",
            excepcion_tipo=result.error_tipo,
            mensaje=result.error_mensaje or "Error genérico",
            stack_trace=result.error_stack,
            resuelto=False
        )
        db.add(err_runtime)

    await db.commit()
    
    return ChatResponse(
        respuesta=result.respuesta,
        fuente=result.fuente,
        escalada_hitl=result.escalada_hitl,
        latencia_ms=lat_total,
        carrito=result.carrito
    )