# app/services/rag_service.py
import re
import json
import asyncio
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
from app.services.faq_service import Candidata, elegir_faq, indice_faq
from app.services.producto_service import cerrar_con_producto, elegir_producto, linea_producto
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

# Cultivos que, si la consulta los nombra, hacen innecesario sumar el del perfil a la búsqueda.
REGEX_CULTIVO_EN_CONSULTA = re.compile(
    r'\b(caf[eé]s?|cafetal\w*|cafeto|ma[ií]z|milpa|frijol\w*|tomate\w*|chiles?|pimient\w+|papas?|repollo\w*|'
    r'cebolla\w*|hortaliza\w*|lechuga\w*|zanahoria\w*|br[oó]coli|pepino\w*|mel[oó]n\w*|sand[ií]a\w*|'
    r'arroz|sorgo|soya|man[ií]|cacao\w*|banano\w*|pl[aá]tano\w*|c[ií]tric\w+|naranj\w+|lim[oó]n\w*|'
    r'aguacate\w*|pasto\w*|potrero\w*|ganado|vacas?|almácigo\w*|almacigo\w*|vivero\w*|ca[ñn]a)\b',
    re.IGNORECASE,
)
# Siglas de formulación que no forman parte del nombre que dice el cliente ("Mancuerna Plus 28 SE").
_FORMULACIONES = {"sc", "ec", "sl", "wp", "wg", "se", "od", "gr", "gb", "sp", "fs", "ew", "cs", "ft", "tds"}
TTL_PRODUCTOS_S = 3600
_NOMBRES_GENERICOS = {"insecticida", "fungicida", "herbicida", "fertilizante", "semilla", "foliar", "abono", "producto"}


def _clave_nombre(texto: str) -> str:
    return re.sub(r"[^a-z0-9]", "", texto.lower())


def nombre_corto_producto(producto: str) -> str:
    """'MANCUERNA PLUS 28 SE' -> 'mancuernaplus'; 'ROOT-PLUS' -> 'rootplus'."""
    palabras = [w for w in re.split(r"[\s\-_/®™]+", producto or "")
                if w and not any(ch.isdigit() for ch in w) and w.lower() not in _FORMULACIONES]
    return _clave_nombre("".join(palabras))


def texto_de_busqueda(consulta: str, cultivo: Optional[str], productos: set) -> str:
    """
    Suma el cultivo del perfil ("... en café") solo si la consulta no nombra ya un cultivo
    ni un producto. Medido con el corpus v2 (2026-10-17): "¿qué tiene para la roya?" + café
    trae los fungicidas de roya; pero sumarlo a "mosca blanca en tomate" o a "¿cuánto Root
    Plus...?" trae productos de café. La forma vieja (" Cultivo: café.") traía fichas de
    fertilizantes cuyo texto es casi solo "Cultivo: café.".
    """
    if not cultivo or REGEX_CULTIVO_EN_CONSULTA.search(consulta):
        return consulta
    clave = _clave_nombre(consulta)
    if any(nombre in clave for nombre in productos):
        return consulta
    return f"{consulta} en {cultivo}"


UMBRAL_CONFIANZA: Dict[str, float] = {
    "informativa": settings.confidence_threshold_informational,
    "recomendacion": settings.confidence_threshold_recommendation,
}

# System prompt, plantillas, mensajes de fallback y parámetros LLM viven en
# atv.prompt_config (ver app/services/prompt_config.py). Se leen en cada consulta.


_CACHE_PRODUCTOS: Optional[tuple] = None  # (monotonic, set de nombres cortos)


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

    async def _nombres_productos(self) -> set:
        """Nombres cortos de los productos del corpus (para texto_de_busqueda). Caché por proceso, 1 h."""
        global _CACHE_PRODUCTOS
        if _CACHE_PRODUCTOS and time.monotonic() - _CACHE_PRODUCTOS[0] < TTL_PRODUCTOS_S:
            return _CACHE_PRODUCTOS[1]
        nombres, offset = set(), None
        try:
            while True:
                puntos, offset = await self.qdrant.scroll(
                    self.alias_name, limit=500, offset=offset, with_payload=["producto"], with_vectors=False
                )
                nombres.update(nombre_corto_producto(str((p.payload or {}).get("producto") or "")) for p in puntos)
                if offset is None:
                    break
        except Exception as exc:
            logger.warning(f"Could not list corpus products ({type(exc).__name__}): {exc}")
            return _CACHE_PRODUCTOS[1] if _CACHE_PRODUCTOS else set()
        # Nombres muy cortos o genéricos (hay una ficha cuyo "producto" es "INSECTICIDA") darían falsos positivos
        nombres = {n for n in nombres if len(n) >= 5 and n not in _NOMBRES_GENERICOS}
        _CACHE_PRODUCTOS = (time.monotonic(), nombres)
        return nombres

    async def _buscar_fichas(self, consulta: str, cultivo: Optional[str], top_k: int) -> list:
        """Fichas técnicas más parecidas en Qdrant (consulta + cultivo del perfil si hace falta). Lanza si Qdrant falla."""
        texto_busqueda = texto_de_busqueda(consulta, cultivo, await self._nombres_productos()) if cultivo else consulta
        query_vector = list(self.embedding_model.embed([texto_busqueda]))[0].tolist()
        vector_name = await self._resolve_vector_name()
        qdrant_query_vector = (
            NamedVector(name=vector_name, vector=query_vector) if vector_name is not None else query_vector
        )
        return list(await self.qdrant.search(
            collection_name=self.alias_name, query_vector=qdrant_query_vector, limit=top_k
        ))

    async def _generar(self, user_prompt: str, cfg: PromptConfig) -> tuple:
        """(texto, tokens_entrada, tokens_salida) de la generación. Lanza si el LLM falla."""
        response = await self.llm_client.chat.completions.create(
            model=cfg.llm_modelo,
            messages=[
                {"role": "system", "content": cfg.system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=cfg.llm_temperature,
            max_tokens=cfg.llm_max_tokens,
            timeout=settings.llm_timeout_seconds,
        )
        usage = response.usage
        return (response.choices[0].message.content.strip(),
                usage.prompt_tokens if usage else 0, usage.completion_tokens if usage else 0)

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

    async def _responder_faq(
        self,
        faq: Candidata,
        consulta_sanitizada: str,
        intencion: str,
        perfil_texto: str,
        cultivo: Optional[str],
        cfg: PromptConfig,
        lat_sanitizacion: int,
        lat_clasif: int,
        lat_faq: int,
    ) -> RAGResult:
        """
        Responde con la respuesta aprobada de la pregunta frecuente, adaptada por el LLM
        al tono y al perfil, y cierra con un producto de AldeaZON tomado de las fichas más
        parecidas (regla 6 del system_prompt). Si el LLM falla o mete una dosis que no está
        ni en la respuesta aprobada ni en esas fichas, se entrega la respuesta aprobada tal
        cual: ya la validó Asistencia Técnica.
        """
        f = faq.faq
        fuente = f"Preguntas frecuentes de Asistencia Técnica, {f.codigo}"
        registros = [{"tipo": "pregunta_frecuente", "codigo": f.codigo, "similitud": round(faq.similitud, 4)}]

        t_ret = time.perf_counter()
        try:
            fichas = [h.payload or {} for h in await self._buscar_fichas(consulta_sanitizada, cultivo, cfg.retrieval_top_k)]
        except Exception as exc:
            # Sin fichas la FAQ responde igual; el LLM cierra invitando a la tienda (regla 6).
            logger.warning(f"Product lookup for FAQ failed ({type(exc).__name__}): {exc}")
            fichas = []
        lat_faq += int((time.perf_counter() - t_ret) * 1000)
        registros += fichas
        productos = "\n".join(
            cfg.chunk_template.format(**{campo: p.get(campo) for campo in CAMPOS_CHUNK}) for p in fichas
        ) or "Sin fichas de productos para esta consulta."

        user_prompt = cfg.faq_user_prompt_template.format(
            codigo=f.codigo,
            pregunta=f.pregunta,
            respuesta=f.respuesta,
            evitar=f.respuesta_evitar or "—",
            productos=productos,
            consulta=consulta_sanitizada,
            perfil=perfil_texto,
        )

        t_llm = time.perf_counter()
        respuesta_texto, modelo = None, f"{cfg.llm_modelo}_faq"
        tokens_in = tokens_out = 0
        # El producto lo elige una llamada aparte, en paralelo (producto_service.py).
        generacion, ficha_producto = await asyncio.gather(
            self._generar(user_prompt, cfg),
            elegir_producto(self.llm_client, cfg, consulta_sanitizada, cultivo, fichas),
            return_exceptions=True,
        )
        if isinstance(ficha_producto, BaseException):
            ficha_producto = None
        if isinstance(generacion, BaseException):
            logger.warning(f"FAQ generation failed, serving approved answer ({type(generacion).__name__}): {generacion}")
        else:
            respuesta_texto, tokens_in, tokens_out = generacion

        aprobada = f"{f.respuesta}\n\n[Fuente: {fuente}]"
        if respuesta_texto is None:
            respuesta_texto, modelo = aprobada, "faq_literal"
        elif REGEX_ABSTENCION.search(respuesta_texto) or not validar_groundedness_dosis(
            respuesta_texto, f"{f.respuesta} {f.respuesta_evitar or ''} {productos}"
        ):
            motivo = "abstención" if REGEX_ABSTENCION.search(respuesta_texto) else "dosis no respaldada"
            logger.warning(f"FAQ generation rejected for {f.codigo} ({motivo}), serving approved answer")
            respuesta_texto, modelo = aprobada, "faq_literal"

        # La FAQ siempre cierra con producto: el de la ficha elegida o el mensaje de la tienda.
        respuesta_texto = cerrar_con_producto(respuesta_texto, linea_producto(ficha_producto, cfg))
        if ficha_producto:
            registros.append({"tipo": "producto_recomendado", "producto": ficha_producto.get("producto"),
                              "documento_fuente": ficha_producto.get("documento_fuente")})

        return RAGResult(
            consulta_sanitizada=consulta_sanitizada,
            respuesta=respuesta_texto,
            fuente=fuente,
            confianza=min(max(faq.similitud, 0.0), 1.0),
            intencion=intencion,
            escalada_hitl=False,
            modelo_utilizado=modelo,
            registros_usados=registros,
            tokens_entrada=tokens_in,
            tokens_salida=tokens_out,
            lat_sanitizacion_ms=lat_sanitizacion,
            lat_clasif_ms=lat_clasif,
            lat_retrieval_ms=lat_faq,
            lat_llm_ms=int((time.perf_counter() - t_llm) * 1000),
        )

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

        # Phase 2.1: Preguntas frecuentes (atv.pregunta_frecuente) = punto de partida.
        # Candidatas por embedding; el juez LLM corre en paralelo con el scope gate.
        t_faq = time.perf_counter()
        candidatas: List[Candidata] = []
        if cfg.faq_candidatos > 0:
            try:
                await indice_faq.sincronizar(self.db, self.embedding_model)
                candidatas = indice_faq.candidatas(self.embedding_model, consulta_sanitizada, cfg.faq_candidatos)
            except Exception as exc:
                # Sin preguntas frecuentes se sigue con las fichas: no tumba la consulta.
                logger.error(f"FAQ index unavailable ({type(exc).__name__}): {exc}")
                await self.db.rollback()
        lat_faq = int((time.perf_counter() - t_faq) * 1000)

        # Phase 2.2: Scope gate. Separate call so the generation LLM never has to choose
        # between "out of scope" and "not in the sheets" (gpt-4o-mini mixed them up and
        # refused agronomic questions whose retrieved context did not match).
        t_alcance = time.perf_counter()
        en_alcance, faq = await asyncio.gather(
            self._en_alcance(consulta_sanitizada, cfg),
            elegir_faq(self.llm_client, cfg, consulta_sanitizada, cultivo_principal, candidatas),
        )
        lat_clasif += int((time.perf_counter() - t_alcance) * 1000)
        # Una pregunta frecuente es del alcance por definición, aunque el gate dude.
        if faq is None and not en_alcance:
            return RAGResult(
                consulta_sanitizada=consulta_sanitizada,
                respuesta=cfg.msg_fuera_alcance,
                confianza=0.0,
                intencion="fuera_alcance",
                escalada_hitl=False,
                modelo_utilizado="scope_gate",
                lat_sanitizacion_ms=lat_sanitizacion,
                lat_clasif_ms=lat_clasif,
            )

        if faq is not None:
            return await self._responder_faq(
                faq, consulta_sanitizada, intencion, perfil_texto, cultivo_principal, cfg,
                lat_sanitizacion=lat_sanitizacion, lat_clasif=lat_clasif, lat_faq=lat_faq,
            )

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
             str(cfg.llm_temperature), str(cfg.retrieval_top_k),
             cfg.producto_selector_prompt, cfg.producto_linea_template, cfg.msg_producto_tienda]
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
            search_result = await self._buscar_fichas(consulta_sanitizada, cultivo_principal, cfg.retrieval_top_k)
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

        # Phase 6: Asynchronous LLM Generation (+ product selector in parallel, producto_service.py)
        t_llm = time.perf_counter()
        generacion, ficha_producto = await asyncio.gather(
            self._generar(user_prompt, cfg),
            elegir_producto(self.llm_client, cfg, consulta_sanitizada, cultivo_principal, metadata_list),
            return_exceptions=True,
        )
        if isinstance(ficha_producto, BaseException):
            ficha_producto = None
        try:
            if isinstance(generacion, BaseException):
                raise generacion
            respuesta_texto, tokens_in, tokens_out = generacion
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

        # Phase 7.5: cierre con producto de AldeaZON (no tras abstención ni rechazo: ya retornaron)
        respuesta_texto = cerrar_con_producto(respuesta_texto, linea_producto(ficha_producto, cfg))

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