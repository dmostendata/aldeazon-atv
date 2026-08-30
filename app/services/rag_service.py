# app/services/rag_service.py
import re
import time
import logging
import traceback
import httpx
from typing import Optional, List, Dict, Any

from pydantic import BaseModel, Field
from qdrant_client import AsyncQdrantClient
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.pii_sanitizer import sanitizar_pii, clasificar_intencion
from app.services.catalog import resolver_productos_comerciales, construir_carrito_items
from app.schemas.domain import CarritoItem

from app.services.cache_service import semantic_cache
from fastembed import TextEmbedding

logger = logging.getLogger("atv.rag_orchestrator")

# ODAI Blueprint §6.5.3: Exact dosage regex for groundedness verification
REGEX_DOSIS_VALIDACION = re.compile(
    r'\d[\d.,]*\s*(?:-\s*\d[\d.,]*\s*)?\s*(?:kg|g|l|lt|ml|cc|%)\s*/\s*(?:ha|mz)\b',
    re.IGNORECASE
)

REGEX_ABSTENCION = re.compile(
    r'(No\s+tengo\s+(esa\s+)?informaci[oó]n|fichas\s+vigentes|Asistencia\s+T[eé]cnica)',
    re.IGNORECASE
)

UMBRAL_CONFIANZA: Dict[str, float] = {
    "informativa": settings.confidence_threshold_informational,
    "recomendacion": settings.confidence_threshold_recommendation,
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
    """Data Transfer Object mapping execution results back to the FastAPI Controller."""
    consulta_sanitizada: str
    respuesta: str
    fuente: Optional[str] = None
    confianza: float
    intencion: str
    escalada_hitl: bool = False
    modelo_utilizado: str = "gpt-4o-mini"
    registros_usados: List[Dict[str, Any]] = Field(default_factory=list)
    carrito: List[CarritoItem] = Field(default_factory=list)
    
    tokens_entrada: int = 0
    tokens_salida: int = 0
    lat_sanitizacion_ms: int = 0
    lat_clasif_ms: int = 0
    lat_retrieval_ms: int = 0
    lat_llm_ms: int = 0
    
    error_tipo: Optional[str] = None
    error_mensaje: Optional[str] = None
    error_stack: Optional[str] = None
    componente_fallo: Optional[str] = None


def validar_groundedness_dosis(respuesta_llm: str, contexto_completo: str) -> bool:
    """
    Groundedness Gate (RSK-001): Rejects LLM generation if any numerical dosage
    in the response does not appear verbatim in the Qdrant context chunks.
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
    Asynchronous RAG Orchestrator handling NLP flow and cross-service coordination.
    """
    def __init__(self, qdrant_client: AsyncQdrantClient, embedding_model: TextEmbedding, db_session: AsyncSession) -> None:
        self.qdrant = qdrant_client
        self.embedding_model = embedding_model
        self.db = db_session
        self.alias_name = settings.qdrant_collection
        
        # httpx client injected directly to prevent 'proxies' kwarg collisions
        custom_http_client = httpx.AsyncClient()
        self.llm_client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            http_client=custom_http_client
        )

    async def procesar_consulta(self, consulta_raw: str, sucursal_id: int) -> RAGResult:
        # Phase 1: PII Sanitization (Ley 787 Compliance)
        consulta_sanitizada, lat_sanitizacion = sanitizar_pii(consulta_raw)
        
        # Phase 2: Intent Classification
        intencion, lat_clasif = clasificar_intencion(consulta_sanitizada)

        # =================================================================
        # NEW: Phase 2.5: Redis Semantic Cache Interception
        # =================================================================
        # ODAI Blueprint §6.6.6: Only cache informational queries to prevent 
        # locking in outdated chemical recommendations (RSK-001).
        version_corpus_actual = 1 # In production, query this from `atv.inventario_archivo`
        
        if intencion == "informativa":
            t_cache = time.perf_counter()
            cached_data = await semantic_cache.get_cached_response(consulta_sanitizada, version_corpus_actual)
            
            if cached_data:
                lat_cache = int((time.perf_counter() - t_cache) * 1000)
                # Rehydrate the Pydantic model from the cached dictionary
                return RAGResult(
                    consulta_sanitizada=consulta_sanitizada,
                    respuesta=cached_data["respuesta"],
                    fuente=cached_data["fuente"],
                    confianza=cached_data["confianza"],
                    intencion=intencion,
                    escalada_hitl=False,
                    modelo_utilizado="redis_semantic_cache",
                    registros_usados=cached_data["registros_usados"],
                    carrito=[], # Do not cache live commercial inventory
                    tokens_entrada=0,
                    tokens_salida=0,
                    lat_sanitizacion_ms=lat_sanitizacion,
                    lat_clasif_ms=lat_clasif,
                    lat_retrieval_ms=lat_cache,
                    lat_llm_ms=0
                )
        
        # Phase 3: Qdrant Asynchronous Retrieval
        t_ret = time.perf_counter()
        try:
            embedded_result = list(self.embedding_model.embed([consulta_sanitizada]))
            query_vector = embedded_result[0].tolist()

            # 2. Use .search() for explicit vector lookups, NOT .query()
            search_result = await self.qdrant.search(
                collection_name=self.alias_name,
                query_vector=("dense", query_vector),
                limit=3
            )
        except Exception as exc:
            lat_retrieval = int((time.perf_counter() - t_ret) * 1000)
            logger.error(f"Async Qdrant retrieval error: {str(exc)}\n{traceback.format_exc()}")
            return RAGResult(
                consulta_sanitizada=consulta_sanitizada,
                respuesta="El servicio de consulta técnica está temporalmente indisponible. Consulta con Asistencia Técnica.",
                confianza=0.0,
                intencion=intencion,
                escalada_hitl=True,
                modelo_utilizado="none",
                lat_sanitizacion_ms=lat_sanitizacion,
                lat_clasif_ms=lat_clasif,
                lat_retrieval_ms=lat_retrieval,
                error_tipo=type(exc).__name__,
                error_mensaje=str(exc),
                componente_fallo="qdrant_retrieval"
            )
        lat_retrieval = int((time.perf_counter() - t_ret) * 1000)

        hits = list(search_result)
        max_score = float(hits[0].score) if hits else 0.0
        umbral_requerido = UMBRAL_CONFIANZA.get(intencion, 0.55)

        # Phase 4: Confidence Gate (Escalate to HITL if score is too low)
        if max_score < umbral_requerido or not hits:
            return RAGResult(
                consulta_sanitizada=consulta_sanitizada,
                respuesta="No tengo suficiente información respaldada en el corpus para esta recomendación. Valida directamente con el Área de Asistencia Técnica.",
                confianza=max_score,
                intencion=intencion,
                escalada_hitl=True,
                modelo_utilizado="hitl_rule",
                lat_sanitizacion_ms=lat_sanitizacion,
                lat_clasif_ms=lat_clasif,
                lat_retrieval_ms=lat_retrieval
            )

        # Phase 5: LLM Context Construction
        contexto_chunks = []
        metadata_list = []
        doc_top1 = "Ficha_Desconocida.pdf"
        ver_top1 = 1
        principio_activo_top1 = None

        for idx, hit in enumerate(hits):
            payload = hit.metadata or {}
            if idx == 0:
                doc_top1 = payload.get("documento_fuente", "documento.pdf")
                ver_top1 = payload.get("version_corpus", 1)
                principio_activo_top1 = payload.get("principio_activo")
            
            chunk_str = (
                f"[Documento: {payload.get('documento_fuente')} | "
                f"Producto: {payload.get('producto')} | "
                f"Cultivo: {payload.get('cultivo')} | Dosis: {payload.get('dosis')} | "
                f"Condiciones: {payload.get('condiciones')}]"
            )
            contexto_chunks.append(chunk_str)
            metadata_list.append(payload)

        fuente_formateada = f"{doc_top1}, versión {ver_top1}"
        contexto_completo = "\n".join(contexto_chunks)
        user_prompt = f"CONTEXTO RECUPERADO:\n{contexto_completo}\n\nCONSULTA DEL VENDEDOR:\n{consulta_sanitizada}"

        # Phase 6: Asynchronous LLM Generation
        t_llm = time.perf_counter()
        try:
            response = await self.llm_client.chat.completions.create(
                model=settings.llm_primary_model,
                messages=[
                    {"role": "system", "content": SYSTEM_ATV},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.2,
                max_tokens=250,
                timeout=settings.llm_timeout_seconds
            )
            respuesta_texto = response.choices[0].message.content.strip()
            tokens_in = response.usage.prompt_tokens if response.usage else 0
            tokens_out = response.usage.completion_tokens if response.usage else 0
        except Exception as exc:
            lat_llm = int((time.perf_counter() - t_llm) * 1000)
            return RAGResult(
                consulta_sanitizada=consulta_sanitizada,
                respuesta="Error temporal de conexión con el generador técnico. Valida la dosis con Asistencia Técnica.",
                fuente=fuente_formateada,
                confianza=max_score,
                intencion=intencion,
                escalada_hitl=True,
                modelo_utilizado="fallback_hitl",
                lat_sanitizacion_ms=lat_sanitizacion,
                lat_clasif_ms=lat_clasif,
                lat_retrieval_ms=lat_retrieval,
                lat_llm_ms=lat_llm,
                error_tipo=type(exc).__name__,
                error_mensaje=str(exc),
                componente_fallo="llm_generation"
            )
        lat_llm = int((time.perf_counter() - t_llm) * 1000)

        # Phase 7: Groundedness & Abstention Gates
        if REGEX_ABSTENCION.search(respuesta_texto):
            return RAGResult(
                consulta_sanitizada=consulta_sanitizada,
                respuesta=respuesta_texto,
                fuente=fuente_formateada,
                confianza=max_score,
                intencion=intencion,
                escalada_hitl=True,  # Abstention triggers HITL workflow
                modelo_utilizado=f"{settings.llm_primary_model}_abstencion",
                registros_usados=metadata_list,
                tokens_entrada=tokens_in,
                tokens_salida=tokens_out,
                lat_sanitizacion_ms=lat_sanitizacion,
                lat_clasif_ms=lat_clasif,
                lat_retrieval_ms=lat_retrieval,
                lat_llm_ms=lat_llm
            )

        if not validar_groundedness_dosis(respuesta_texto, contexto_completo):
            return RAGResult(
                consulta_sanitizada=consulta_sanitizada,
                respuesta="La dosis generada requiere verificación técnica. Consulta con el Área de Asistencia Técnica antes de aplicarla.",
                fuente=fuente_formateada,
                confianza=max_score,
                intencion=intencion,
                escalada_hitl=True,
                modelo_utilizado=f"{settings.llm_primary_model}_rejected",
                registros_usados=metadata_list,
                tokens_entrada=tokens_in,
                tokens_salida=tokens_out,
                lat_sanitizacion_ms=lat_sanitizacion,
                lat_clasif_ms=lat_clasif,
                lat_retrieval_ms=lat_retrieval,
                lat_llm_ms=lat_llm,
                error_tipo="GroundednessBreach",
                componente_fallo="groundedness_gate"
            )

        # Phase 8: Commercial Sales Resolution (RSK-NEW-04)
        # If the technical query passed all gates and is a recommendation, resolve the cart payload.
        carrito = []
        if intencion == "recomendacion" and principio_activo_top1:
            productos_comerciales = await resolver_productos_comerciales(
                session=self.db,
                principio_activo=principio_activo_top1,
                sucursal_id=sucursal_id
            )
            carrito = construir_carrito_items(productos_comerciales)

        # =================================================================
        # NEW: Phase 9: Cache the Final Result
        # =================================================================
        if intencion == "informativa" and not escalada_hitl:
            await semantic_cache.set_cached_response(
                consulta_sanitizada=consulta_sanitizada,
                version_corpus=version_corpus_actual,
                response_payload={
                    "respuesta": respuesta_texto,
                    "fuente": fuente_formateada,
                    "confianza": max_score,
                    "registros_usados": metadata_list
                }
            )

        return RAGResult(
            consulta_sanitizada=consulta_sanitizada,
            respuesta=respuesta_texto,
            fuente=fuente_formateada,
            confianza=max_score,
            intencion=intencion,
            escalada_hitl=False,
            modelo_utilizado=settings.llm_primary_model,
            registros_usados=metadata_list,
            carrito=carrito,
            tokens_entrada=tokens_in,
            tokens_salida=tokens_out,
            lat_sanitizacion_ms=lat_sanitizacion,
            lat_clasif_ms=lat_clasif,
            lat_retrieval_ms=lat_retrieval,
            lat_llm_ms=lat_llm
        )