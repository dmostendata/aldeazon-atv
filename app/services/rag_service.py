# app/services/rag_service.py
import re
import json
import time
import hashlib
import logging
import traceback
import httpx
from typing import Optional, List, Dict, Any

from pydantic import BaseModel, Field
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import NamedVector
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.pii_sanitizer import sanitizar_pii, clasificar_intencion
from app.services.catalog import resolver_productos_comerciales, construir_carrito_items
from app.services.prompt_config import cargar_prompt_config, CAMPOS_CHUNK, PromptConfig
from app.services.perfil_cliente import PerfilUsuario
from app.schemas.domain import CarritoItem

from app.services.cache_service import semantic_cache
from fastembed import TextEmbedding

logger = logging.getLogger("atv.rag_orchestrator")

# ODAI Blueprint §6.5.3: Exact dosage regex for groundedness verification
REGEX_DOSIS_VALIDACION = re.compile(
    r'\d[\d.,]*\s*(?:-\s*\d[\d.,]*\s*)?\s*(?:kg|g|l|lt|ml|cc|%)\s*/\s*(?:ha|mz)\b',
    re.IGNORECASE
)

# Only the mandated abstention phrase ('No tengo esa información en las fichas vigentes...').
# "Asistencia Técnica" is NOT a signal: the system prompt requires it in every
# recommendation ("Valide esta recomendación con Asistencia Técnica..."), which
# escalated every recommendation to HITL and always emptied the cart.
REGEX_ABSTENCION = re.compile(
    r'(No\s+tengo\s+(esa\s+)?informaci[oó]n|fichas\s+vigentes)',
    re.IGNORECASE
)

UMBRAL_CONFIANZA: Dict[str, float] = {
    "informativa": settings.confidence_threshold_informational,
    "recomendacion": settings.confidence_threshold_recommendation,
}

# System prompt, plantillas, mensajes de fallback y parámetros LLM viven en
# atv.prompt_config (ver app/services/prompt_config.py). Se leen en cada consulta.


class RAGResult(BaseModel):
    """Data Transfer Object mapping execution results back to the FastAPI Controller."""
    consulta_sanitizada: str
    respuesta: str
    fuente: Optional[str] = None
    confianza: float
    intencion: str
    escalada_hitl: bool = False
    modelo_utilizado: str = ""
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


def _extraer_nombre_vector(vectors_config: Any) -> Optional[str]:
    """
    Given a Qdrant collection's `config.params.vectors`, returns the name of
    the (single) named vector if one is configured, or None if the
    collection uses the default unnamed vector.

    The ingestion pipeline creates collections via
    `qdrant.get_fastembed_vector_params()`, which auto-names the vector
    after the embedding model (e.g. "fast-multilingual-e5-large"). Search
    requests must target that same name explicitly — passing a bare vector
    targets Qdrant's unnamed "" vector slot, which doesn't exist in that
    config and raises a 400.
    """
    if isinstance(vectors_config, dict):
        nombres = list(vectors_config.keys())
        if not nombres:
            return None
        if len(nombres) > 1:
            logger.warning(
                f"Collection has multiple named vectors {nombres}; "
                f"defaulting to the first one ('{nombres[0]}')."
            )
        return nombres[0]
    # A single unnamed VectorParams object means the collection was created
    # WITHOUT fastembed's naming convention -> use the bare/default vector.
    return None


class ATVRagService:
    """
    Asynchronous RAG Orchestrator handling NLP flow and cross-service coordination.
    """
    def __init__(self, qdrant_client: AsyncQdrantClient, embedding_model: TextEmbedding, db_session: AsyncSession) -> None:
        self.qdrant = qdrant_client
        self.embedding_model = embedding_model
        self.db = db_session
        self.alias_name = settings.qdrant_collection

        # Resolved lazily on first query, then cached for the life of this
        # instance. See _resolve_vector_name() for why this can't just be
        # a hardcoded string.
        self._vector_name: Optional[str] = None
        self._vector_name_resolved: bool = False

        # httpx client injected directly to prevent 'proxies' kwarg collisions
        custom_http_client = httpx.AsyncClient()
        self.llm_client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            http_client=custom_http_client
        )

    async def _resolve_vector_name(self) -> Optional[str]:
        """
        Fetches and caches the name of the named vector configured on the
        aliased Qdrant collection. Resolved once per service instance
        (cheap metadata call) rather than hardcoded, so this doesn't
        silently break again if the embedding model or the collection's
        naming convention changes on a future corpus rebuild.
        """
        if self._vector_name_resolved:
            return self._vector_name

        try:
            collection_info = await self.qdrant.get_collection(self.alias_name)
            vectors_config = collection_info.config.params.vectors
            self._vector_name = _extraer_nombre_vector(vectors_config)
        except Exception as exc:
            logger.warning(
                f"Could not resolve named-vector config for '{self.alias_name}', "
                f"falling back to unnamed vector search: {exc}"
            )
            self._vector_name = None

        self._vector_name_resolved = True
        return self._vector_name

    async def _en_alcance(self, consulta_sanitizada: str, cfg: PromptConfig) -> bool:
        """True if the query is about agriculture or Aldea Global. Fails open (True) on any error."""
        try:
            response = await self.llm_client.chat.completions.create(
                model=cfg.llm_modelo,
                messages=[
                    {"role": "system", "content": cfg.alcance_prompt},
                    {"role": "user", "content": consulta_sanitizada},
                ],
                temperature=0,
                max_tokens=20,
                response_format={"type": "json_object"},
                timeout=settings.llm_timeout_seconds,
            )
            return json.loads(response.choices[0].message.content).get("en_alcance") is not False
        except Exception as exc:
            logger.warning(f"Scope gate failed open ({type(exc).__name__}): {exc}")
            return True

    async def procesar_consulta(
        self,
        consulta_raw: str,
        sucursal_id: Optional[int],
        perfil: Optional[PerfilUsuario] = None,
        cfg: Optional[PromptConfig] = None,
    ) -> RAGResult:
        # Phase 0: Prompt configuration from PostgreSQL (single source of truth).
        # PromptConfigError is intentionally NOT caught here: the endpoint maps it to HTTP 503.
        if cfg is None:
            cfg = await cargar_prompt_config(self.db)

        # Perfil del usuario (perfilamiento.py): entra al prompt, a la búsqueda y a la caché.
        perfil_texto = perfil.resumen_para_prompt() if perfil else "Sin perfil del usuario."
        cultivo_principal = perfil.cultivo_principal() if perfil else None

        # Phase 1: PII Sanitization (Ley 787 Compliance)
        consulta_sanitizada, lat_sanitizacion = sanitizar_pii(consulta_raw)
        
        # Phase 2: Intent Classification
        intencion, lat_clasif = clasificar_intencion(consulta_sanitizada)

        # Phase 2.1: Scope gate. Separate call so the generation LLM never has to choose
        # between "out of scope" and "not in the sheets" (gpt-4o-mini mixed them up and
        # refused agronomic questions whose retrieved context did not match).
        t_alcance = time.perf_counter()
        if not await self._en_alcance(consulta_sanitizada, cfg):
            return RAGResult(
                consulta_sanitizada=consulta_sanitizada,
                respuesta=cfg.msg_fuera_alcance,
                confianza=0.0,
                intencion="fuera_alcance",
                escalada_hitl=False,
                modelo_utilizado="scope_gate",
                lat_sanitizacion_ms=lat_sanitizacion,
                lat_clasif_ms=lat_clasif + int((time.perf_counter() - t_alcance) * 1000),
            )
        lat_clasif += int((time.perf_counter() - t_alcance) * 1000)

        # =================================================================
        # NEW: Phase 2.5: Redis Semantic Cache Interception
        # =================================================================
        # ODAI Blueprint §6.6.6: Only cache informational queries to prevent 
        # locking in outdated chemical recommendations (RSK-001).
        version_corpus_actual = 1 # In production, query this from `atv.inventario_archivo`
        
        # La respuesta depende del perfil y de los prompts: dos perfiles distintos no
        # comparten caché, y editar atv.prompt_config invalida lo cacheado con la versión
        # anterior (si no, una respuesta vieja se sirve hasta 7 días después del cambio).
        huella_prompts = hashlib.sha256("\x1f".join(
            [cfg.system_prompt, cfg.user_prompt_template, cfg.chunk_template, cfg.llm_modelo,
             str(cfg.llm_temperature), str(cfg.retrieval_top_k)]
        ).encode("utf-8")).hexdigest()[:12]
        clave_cache = f"{consulta_sanitizada}||{perfil_texto}||{huella_prompts}"

        if intencion == "informativa":
            t_cache = time.perf_counter()
            cached_data = await semantic_cache.get_cached_response(clave_cache, version_corpus_actual)
            
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
                    carrito=[],
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
            texto_busqueda = (
                f"{consulta_sanitizada} Cultivo: {cultivo_principal}." if cultivo_principal else consulta_sanitizada
            )
            embedded_result = list(self.embedding_model.embed([texto_busqueda]))
            query_vector = embedded_result[0].tolist()

            vector_name = await self._resolve_vector_name()
            qdrant_query_vector = (
                NamedVector(name=vector_name, vector=query_vector)
                if vector_name is not None
                else query_vector
            )

            search_result = await self.qdrant.search(
                collection_name=self.alias_name,
                query_vector=qdrant_query_vector,
                limit=cfg.retrieval_top_k
            )
        except Exception as exc:
            lat_retrieval = int((time.perf_counter() - t_ret) * 1000)
            logger.error(f"Async Qdrant retrieval error: {str(exc)}\n{traceback.format_exc()}")
            return RAGResult(
                consulta_sanitizada=consulta_sanitizada,
                respuesta=cfg.msg_retrieval_error,
                confianza=0.0,
                intencion=intencion,
                escalada_hitl=True,
                modelo_utilizado="none",
                lat_sanitizacion_ms=lat_sanitizacion,
                lat_clasif_ms=lat_clasif,
                lat_retrieval_ms=lat_retrieval,
                error_tipo=type(exc).__name__,
                error_mensaje=str(exc),
                error_stack=traceback.format_exc(),
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
                respuesta=cfg.msg_baja_confianza,
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
            payload = hit.payload or {}
            if idx == 0:
                doc_top1 = payload.get("documento_fuente", "documento.pdf")
                ver_top1 = payload.get("version_corpus", 1)
                principio_activo_top1 = payload.get("principio_activo")
            
            chunk_str = cfg.chunk_template.format(
                **{campo: payload.get(campo) for campo in CAMPOS_CHUNK}
            )
            contexto_chunks.append(chunk_str)
            metadata_list.append(payload)

        fuente_formateada = f"{doc_top1}, versión {ver_top1}"
        contexto_completo = "\n".join(contexto_chunks)
        user_prompt = cfg.user_prompt_template.format(
            contexto=contexto_completo,
            consulta=consulta_sanitizada,
            perfil=perfil_texto,
        )

        # Phase 6: Asynchronous LLM Generation
        t_llm = time.perf_counter()
        try:
            response = await self.llm_client.chat.completions.create(
                model=cfg.llm_modelo,
                messages=[
                    {"role": "system", "content": cfg.system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=cfg.llm_temperature,
                max_tokens=cfg.llm_max_tokens,
                timeout=settings.llm_timeout_seconds
            )
            respuesta_texto = response.choices[0].message.content.strip()
            tokens_in = response.usage.prompt_tokens if response.usage else 0
            tokens_out = response.usage.completion_tokens if response.usage else 0
        except Exception as exc:
            lat_llm = int((time.perf_counter() - t_llm) * 1000)
            return RAGResult(
                consulta_sanitizada=consulta_sanitizada,
                respuesta=cfg.msg_llm_error,
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
                error_stack=traceback.format_exc(),
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
                modelo_utilizado=f"{cfg.llm_modelo}_abstencion",
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
                respuesta=cfg.msg_groundedness_rechazo,
                fuente=fuente_formateada,
                confianza=max_score,
                intencion=intencion,
                escalada_hitl=True,
                modelo_utilizado=f"{cfg.llm_modelo}_rejected",
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
        sucursal_carrito = sucursal_id or (perfil.sucursal_id if perfil else None)
        if intencion == "recomendacion" and principio_activo_top1 and sucursal_carrito:
            productos_comerciales = await resolver_productos_comerciales(
                session=self.db,
                principio_activo=principio_activo_top1,
                sucursal_id=sucursal_carrito
            )
            # Precio de asociado solo para socios activos identificados; sin perfil
            # (llamada directa de la tienda) se mantiene el comportamiento anterior.
            carrito = construir_carrito_items(
                productos_comerciales,
                aplicar_precio_asociado=perfil.es_asociado_activo if perfil else True,
            )

        # =================================================================
        # NEW: Phase 9: Cache the Final Result
        # =================================================================
        if intencion == "informativa":
            await semantic_cache.set_cached_response(
                consulta_sanitizada=clave_cache,
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
            modelo_utilizado=cfg.llm_modelo,
            registros_usados=metadata_list,
            carrito=carrito,
            tokens_entrada=tokens_in,
            tokens_salida=tokens_out,
            lat_sanitizacion_ms=lat_sanitizacion,
            lat_clasif_ms=lat_clasif,
            lat_retrieval_ms=lat_retrieval,
            lat_llm_ms=lat_llm
        )