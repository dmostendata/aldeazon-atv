# app/main.py
import time
import re
from fastapi import FastAPI, Depends, HTTPException, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.models.traceability import Base, Consulta, ErrorLog

# Ensure the isolated 'atv' schema and tables exist on boot[cite: 17]
with engine.connect() as conn:
    conn.execute(text("CREATE SCHEMA IF NOT EXISTS atv"))
    conn.commit()

Base.metadata.create_all(bind=engine)

app = FastAPI(title="ATV Internal API - AldeaZON")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)


async def verify_api_key(api_key: str = Security(api_key_header)):
    VALID_API_KEY = "aldeazon_internal_app_secret_2026"
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


def sanitizar_pii(texto: str) -> str:
    """
    Strips PII from text before external LLM API calls (Ley 787 compliance) (§6.6.2)[cite: 17].
    """
    texto = re.sub(r'\b\d{3}-?\d{6}-?\d{4}[A-Z]?\b', '[CEDULA]', texto)
    texto = re.sub(r'\b[578]\d{7}\b', '[TELEFONO]', texto)
    return texto


@app.post("/api/v1/consultar", response_model=ChatResponse, dependencies=[Depends(verify_api_key)])
async def procesar_consulta_interna(request: ChatRequest, db: Session = Depends(get_db)):
    start_time = time.time()
    
    # Phase 1: PII Sanitization (Ley 787)[cite: 17]
    t0 = time.time()
    texto_seguro = sanitizar_pii(request.consulta)
    lat_sanitizacion = int((time.time() - t0) * 1000)
    
    # Phase 2: Classification & Retrieval (Mocks representing active Qdrant search)
    t1 = time.time()
    respuesta_rag = "Aplica 1,5 L/ha. Valida con Asistencia Técnica."
    fuente = "ficha_consento.pdf, v1"
    hitl = True
    lat_retrieval = int((time.time() - t1) * 1000)
    
    lat_total = int((time.time() - start_time) * 1000)
    
    # Ensure SLA <= 10,000ms[cite: 17]
    if lat_total > 10000:
        err = ErrorLog(
            componente="atv_api",
            excepcion_tipo="SLABreachError",
            mensaje=f"Presupuesto de latencia excedido: {lat_total}ms",
            resuelto=False
        )
        db.add(err)
    
    # Phase 3: Audit Logging to atv.consulta[cite: 17]
    log_registro = Consulta(
        vendedor_hash=request.vendedor_id,
        consulta_sanitizada=texto_seguro,
        intencion="recomendacion",
        confianza=0.850,
        escalada_hitl=hitl,
        respuesta=respuesta_rag,
        modelo="gpt-4o-mini",
        tokens_entrada=120,
        tokens_salida=45,
        costo_estimado_usd=0.000045,
        lat_webhook_ms=0,
        lat_sanitizacion_ms=lat_sanitizacion,
        lat_retrieval_ms=lat_retrieval,
        lat_llm_ms=50,
        lat_total_ms=lat_total,
        version_corpus=1
    )
    
    db.add(log_registro)
    db.commit()
    db.refresh(log_registro)
    
    return ChatResponse(
        respuesta=respuesta_rag,
        fuente=fuente,
        escalada_hitl=hitl,
        latencia_ms=lat_total
    )