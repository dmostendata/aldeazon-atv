# app/main.py
from fastapi import FastAPI, Depends, HTTPException, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import text # Required for executing raw SQL
import time

from app.core.database import engine, get_db
from app.models.consulta import Base, Consulta

# 1. Create the isolated namespace before migrating the tables
# We use a context manager (with) to safely open and close the raw connection.
with engine.connect() as conn:
    conn.execute(text("CREATE SCHEMA IF NOT EXISTS atv"))
    conn.commit()

# 2. Auto-migrate the tables into the 'atv' schema.
Base.metadata.create_all(bind=engine)

app = FastAPI(title="ATV Internal API")

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)

async def verify_api_key(api_key: str = Security(api_key_header)):
    VALID_API_KEY = "aldeazon_internal_app_secret_2026"
    if api_key != VALID_API_KEY:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    return api_key

class ChatRequest(BaseModel):
    vendedor_id: str = Field(..., description="ID o hash del usuario de la app interna")
    consulta: str = Field(..., min_length=2, max_length=1000)
    sesion_id: str | None = Field(None, description="Para trazabilidad de conversaciones")

class ChatResponse(BaseModel):
    respuesta: str
    fuente: str | None
    escalada_hitl: bool
    latencia_ms: int

@app.post("/api/v1/consultar", response_model=ChatResponse, dependencies=[Depends(verify_api_key)])
async def procesar_consulta_interna(request: ChatRequest, db: Session = Depends(get_db)):
    start_time = time.time()
    
    # 1. Pipeline Execution (Mocks for now)
    respuesta_rag = "Aplica 1,5 L/ha. Valida con Asistencia Técnica."
    fuente = "ficha_consento.pdf, v1"
    hitl = True
    texto_seguro = request.consulta 
    
    latencia_total = int((time.time() - start_time) * 1000)
    
    # 2. Trazabilidad (Event Logging)
    log_registro = Consulta(
        vendedor=request.vendedor_id, # Updated to match the strict model property
        consulta=request.consulta,
        texto_sanitizado=texto_seguro,
        intencion="recomendacion",
        escalada_hitl=hitl,
        respuesta=respuesta_rag,
        lat_total_ms=latencia_total
    )
    
    db.add(log_registro)
    db.commit()
    db.refresh(log_registro)
    
    return ChatResponse(
        respuesta=respuesta_rag,
        fuente=fuente,
        escalada_hitl=hitl,
        latencia_ms=latencia_total
    )