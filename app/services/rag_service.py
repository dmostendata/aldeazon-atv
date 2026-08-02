# app/services/rag_service.py
import re
import time
import logging
import traceback
from typing import Optional, List, Dict, Any, Tuple
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient
from openai import AsyncOpenAI

# Pythonic Pattern: Directly import immutable Pydantic Settings singleton.
from app.core.config import settings

logger = logging.getLogger(__name__)

# ODAI Blueprint §6.6.3: Deterministic risk regex for agricultural recommendations
REGEX_RIESGO = re.compile(
    r'(dosis|cu[aa]nt[oa]s?\s+(aplico|echo|uso)|qu[eé]\s+producto|'
    r'mezclar|aplicar|l(itros)?\s*/\s*(ha|mz)|tratamiento\s+para)',
    re.IGNORECASE
)

# ODAI Blueprint §6.5.3: Exact dosage regex for groundedness verification
REGEX_DOSIS_VALIDACION = re.compile(
    r'\d[\d.,]*\s*(?:-\s*\d[\d.,]*\s*)?\s*(?:kg|g|l|lt|ml|cc|%)\s*(?:/\s*(?:ha|mz))?',
    re.IGNORECASE
)

# ODAI Blueprint §6.6.7: Domain Gate regex to intercept LLM abstention and trigger HITL
REGEX_ABSTENCION = re.compile(
    r'(No\s+tengo\s+(esa\s+)?informaci[oó]n|fichas\s+vigentes|Asistencia\s+T[eé]cnica)',
    re.IGNORECASE
)

UMBRAL_CONFIANZA: Dict[str, float] = {
    "informativa": 0.55,
    "recomendacion": 0.70,
}

SYSTEM_ATV = """
Eres el Asistente Técnico Virtual de AldeaZON. Ayudas a vendedores de tiendas de insumos agrícolas en Nicaragua. Reglas obligatorias:
1. Responde SOLO con la información del CONTEXTO. Si el contexto no contiene la respuesta, di exactamente: 'No tengo esa información en las fichas vigentes. Consulta al Área de Asistencia Técnica.'
2. Nunca inventes dosis, productos ni condiciones. Toda cifra que menciones debe estar literalmente en el contexto.
3. Cierra SIEMPRE con la fuente: [Fuente: {documento}, versión {v}].
4. Responde en español, claro y breve. Máximo ~120 palabras.
5. Si la consulta pide una recomendación de producto o dosis, incluye: 'Valida esta recomendación con Asistencia Técnica antes de aplicarla.'
""".strip()


class RAGResult(BaseModel):
    """
    Pydantic DTO carrying runtime execution results, APM economics, and error telemetry
    """
    respuesta: str
    fuente: Optional[str] = None
    confianza: float
    intencion: str
    escalada_hitl: bool = False
    modelo_utilizado: str = "gpt-4o-mini"
    registros_usados: List[Dict[str, Any]] = Field(default_factory=list)
    tokens_entrada: int = 0
    tokens_salida: int = 0
    lat_sanitizacion_ms: int = 0
    lat_retrieval_ms: int = 0
    lat_llm_ms: int = 0
    
    # Telemetry attributes for PostgreSQL atv.error_log persistence
    error_tipo: Optional[str] = None
    error_mensaje: Optional[str] = None
    error_stack: Optional[str] = None
    componente_fallo: Optional[str] = None


def sanitizar_pii(texto: str) -> Tuple[str, int]:
    """
    Strips PII (cédulas, phone numbers) before sending text to external LLMs (§6.6.2)
    """
    t0 = time.perf_counter()
    texto_seguro = re.sub(r'\b\d{3}-?\d{6}-?\d{4}[A-Z]?\b', '[CEDULA]', texto)
    texto_seguro = re.sub(r'\b[578]\d{7}\b', '[TELEFONO]', texto_seguro)
    lat_ms = int((time.perf_counter() - t0) * 1000)
    return texto_seguro, lat_ms


def clasificar_intencion(texto: str) -> str:
    """
    Deterministic intent classification in <=0.3s without LLM overhead (§6.6.3)
    """
    if REGEX_RIESGO.search(texto):
        return "recomendacion"
    return "informativa"


def validar_groundedness_dosis(respuesta_llm: str, contexto_completo: str) -> bool:
    """
    Groundedness Gate (§6.6.7): Rejects LLM generation if any numerical dosage
    in the response does not appear verbatim in the Qdrant context chunks
    """
    cifras_respuesta = [
        m.group(0).lower().strip() 
        for m in REGEX_DOSIS_VALIDACION.finditer(respuesta_llm)
    ]
    if not cifras_respuesta:
        return True

    contexto_norm = contexto_completo.lower()
    for cifra in cifras_respuesta:
        if cifra not in contexto_norm:
            logger.warning(f"Groundedness breach detected. Hallucinated dosage: '{cifra}'")
            return False
    return True


class ATVRagService:
    """
    Asynchronous RAG Orchestration Service
    """
    def __init__(self, qdrant_client: QdrantClient) -> None:
        self.qdrant = qdrant_client
        self.alias_name = settings.qdrant_collection
        self.model_name = "intfloat/multilingual-e5-large"
        
        # Globally bind the 1024-dim model to this QdrantClient instance.
        # This prevents qdrant_client from falling back to default 384-dim BAAI/bge-small-en.
        self.qdrant.set_model(self.model_name)
        
        self.llm_client = AsyncOpenAI(api_key=settings.openai_api_key)

    async def procesar_consulta(self, consulta_raw: str) -> RAGResult:
        # Phase 1: PII Sanitization (§6.6.2)
        consulta_sanitizada, lat_sanitizacion = sanitizar_pii(consulta_raw)
        
        # Phase 2: Intent Classification (§6.6.3)
        intencion = clasificar_intencion(consulta_sanitizada)
        
        # Phase 3: Qdrant Retrieval via Alias (§6.6.4)
        t_ret_start = time.perf_counter()
        try:
            search_result = self.qdrant.query(
                collection_name=self.alias_name,
                query_text=consulta_sanitizada,
                limit=3
            )
        except Exception as exc:
            # Enforce required latency calculation BEFORE logging error
            lat_retrieval = int((time.perf_counter() - t_ret_start) * 1000)
            logger.error(f"Qdrant retrieval error on alias '{self.alias_name}': {str(exc)}")
            
            return RAGResult(
                respuesta="El servicio de consulta técnica está temporalmente indisponible. Consulta con Asistencia Técnica.",
                confianza=0.0,
                intencion=intencion,
                escalada_hitl=True,
                modelo_utilizado="none",
                lat_sanitizacion_ms=lat_sanitizacion,
                lat_retrieval_ms=lat_retrieval,
                error_tipo=type(exc).__name__,
                error_mensaje=str(exc),
                error_stack=traceback.format_exc(),
                componente_fallo="qdrant_retrieval"
            )
        lat_retrieval = int((time.perf_counter() - t_ret_start) * 1000)

        hits = list(search_result)
        max_score = float(hits[0].score) if hits else 0.0
        umbral_requerido = UMBRAL_CONFIANZA.get(intencion, 0.55)

        # Phase 4: Confidence Gate (§6.6.5) - Escalate to HITL if score is too low[cite: 14]
        if max_score < umbral_requerido or not hits:
            logger.info(
                f"Low confidence ({max_score:.3f} < {umbral_requerido}). "
                "Escalating to HITL (§6.6.5)."
            )
            return RAGResult(
                respuesta="No tengo suficiente información respaldada en el corpus para esta recomendación. Valida directamente con el Área de Asistencia Técnica.",
                confianza=max_score,
                intencion=intencion,
                escalada_hitl=True,
                modelo_utilizado="hitl_rule",
                lat_sanitizacion_ms=lat_sanitizacion,
                lat_retrieval_ms=lat_retrieval
            )

        # Phase 5: LLM Context Construction & Generation (§6.6.7)[cite: 14]
        contexto_chunks = []
        metadata_list = []
        doc_top1 = "Ficha_Desconocida.pdf"
        ver_top1 = 1

        for idx, hit in enumerate(hits):
            payload = hit.metadata or {}
            doc_fuente = payload.get("documento_fuente", "documento.pdf")
            version = payload.get("version_corpus", 1)
            if idx == 0:
                doc_top1 = doc_fuente
                ver_top1 = version
            
            chunk_str = (
                f"[Documento: {doc_fuente} | Versión: {version} | "
                f"Producto: {payload.get('producto', 'N/A')} | "
                f"Cultivo: {payload.get('cultivo', 'N/A')} | Dosis: {payload.get('dosis', 'N/A')} | "
                f"Condiciones: {payload.get('condiciones', '')}]"
            )
            contexto_chunks.append(chunk_str)
            metadata_list.append(payload)

        fuente_formateada = f"{doc_top1}, versión {ver_top1}"
        contexto_completo = "\n---\n".join(contexto_chunks)
        user_prompt = f"CONTEXTO RECUPERADO:\n{contexto_completo}\n\nCONSULTA DEL VENDEDOR:\n{consulta_sanitizada}"

        t_llm_start = time.perf_counter()
        try:
            response = await self.llm_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": SYSTEM_ATV},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.2,
                max_tokens=250,
                timeout=6.0  # ODAI Blueprint §6.7: 6.0s hard timeout[cite: 14]
            )
            respuesta_texto = response.choices[0].message.content.strip()
            tokens_in = response.usage.prompt_tokens if response.usage else 0
            tokens_out = response.usage.completion_tokens if response.usage else 0
        except Exception as exc:
            # Enforce required latency calculation BEFORE logging error
            lat_llm = int((time.perf_counter() - t_llm_start) * 1000)
            logger.error(f"LLM API generation failure: {str(exc)}")
            
            return RAGResult(
                respuesta="Error temporal de conexión con el generador técnico. Valida la dosis con Asistencia Técnica.",
                fuente=fuente_formateada,
                confianza=max_score,
                intencion=intencion,
                escalada_hitl=True,
                modelo_utilizado="fallback_hitl",
                registros_usados=metadata_list,
                lat_sanitizacion_ms=lat_sanitizacion,
                lat_retrieval_ms=lat_retrieval,
                lat_llm_ms=lat_llm,
                error_tipo=type(exc).__name__,
                error_mensaje=str(exc),
                error_stack=traceback.format_exc(),
                componente_fallo="llm_generation"
            )
        lat_llm = int((time.perf_counter() - t_llm_start) * 1000)

        # Domain Gate: Override HITL if the LLM explicitly abstains due to missing context
        if REGEX_ABSTENCION.search(respuesta_texto):
            logger.info("LLM abstained from answering due to missing context. Overriding escalada_hitl = True.")
            return RAGResult(
                respuesta=respuesta_texto,
                fuente=fuente_formateada,
                confianza=max_score,
                intencion=intencion,
                escalada_hitl=True,
                modelo_utilizado="gpt-4o-mini_abstencion",
                registros_usados=metadata_list,
                tokens_entrada=tokens_in,
                tokens_salida=tokens_out,
                lat_sanitizacion_ms=lat_sanitizacion,
                lat_retrieval_ms=lat_retrieval,
                lat_llm_ms=lat_llm
            )

        # Phase 6: Groundedness Verification Gate (§6.6.7)[cite: 14]
        if not validar_groundedness_dosis(respuesta_texto, contexto_completo):
            logger.warning("LLM response failed groundedness gate. Override with HITL escalation.")
            return RAGResult(
                respuesta="La dosis generada requiere verificación técnica. Consulta con el Área de Asistencia Técnica antes de aplicarla.",
                fuente=fuente_formateada,
                confianza=max_score,
                intencion=intencion,
                escalada_hitl=True,
                modelo_utilizado="gpt-4o-mini_rejected",
                registros_usados=metadata_list,
                tokens_entrada=tokens_in,
                tokens_salida=tokens_out,
                lat_sanitizacion_ms=lat_sanitizacion,
                lat_retrieval_ms=lat_retrieval,
                lat_llm_ms=lat_llm,
                error_tipo="GroundednessBreach",
                error_mensaje="El LLM generó una cifra de dosis que no está literalmente en las fichas recuperadas.",
                error_stack="Validacion: validar_groundedness_dosis() devolvio False.",
                componente_fallo="groundedness_gate"
            )

        return RAGResult(
            respuesta=respuesta_texto,
            fuente=fuente_formateada,
            confianza=max_score,
            intencion=intencion,
            escalada_hitl=False,
            modelo_utilizado="gpt-4o-mini",
            registros_usados=metadata_list,
            tokens_entrada=tokens_in,
            tokens_salida=tokens_out,
            lat_sanitizacion_ms=lat_sanitizacion,
            lat_retrieval_ms=lat_retrieval,
            lat_llm_ms=lat_llm
        )