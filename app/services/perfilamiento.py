# app/services/perfilamiento.py
"""
Perfilamiento conversacional: antes de recomendar, el ATV pregunta quién es el
usuario y cómo es su finca o negocio.

Flujo por sesión (atv.sesion_perfil). Regla: TODO lo que hace falta para
analizar el caso se pide junto, en un solo mensaje, para no gastar un turno
(y una llamada al LLM) por dato.
  1. Sesión nueva -> siempre perfil_msg_bienvenida, que pide de una vez: si es
     socio/cliente, cédula + WhatsApp + su consulta; si no, a qué se dedica,
     cultivo y área, zona, etapa, terreno y el problema. Salvo que el primer
     mensaje ya lo identifique como socio/cliente o ya traiga todo el caso
     (p. ej. el formulario inicial de la consola de pruebas).
  2. Cédula + teléfono -> se buscan (hasheados) en atv.perfil_productor. Si
     coinciden, el perfil sale del registro: rubros, fincas, lotes, ubicación.
  3. Lo demás lo extrae un LLM (JSON) del mensaje ya sanitizado. Lo que falte
     (cultivo, ubicación, etapa/terreno, consulta) se pide en UN mensaje
     (perfil_msg_pedir_faltantes); cada dato se pide una sola vez y después se
     responde con lo que haya. perfil_max_preguntas es el tope de turnos.
  Con `contexto` (ContextoInicial, p. ej. el formulario inicial) el perfil llega
  ya estructurado: no se llama al LLM de extracción y `consulta` es la consulta.
  4. Perfil completo -> la consulta técnica que el usuario haya hecho en el
     camino (consulta_pendiente) pasa al RAG con el perfil.
"""
import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import List, Optional

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.traceability import SesionPerfil
from app.schemas.domain import ContextoInicial
from app.services.perfil_cliente import (
    TIPOS_SIN_FINCA, TIPOS_USUARIO, Cultivo, PerfilUsuario, area_a_mz, buscar_persona_id, cargar_perfil_registrado,
    extraer_cedula, extraer_telefonos, hashes, ocultar_telefonos,
)
from app.services.pii_sanitizer import REGEX_CEDULA, sanitizar_pii
from app.services.prompt_config import PromptConfig
from app.services.traza import paso

logger = logging.getLogger("atv.perfilamiento")

# Intentos de cédula + teléfono por sesión: frena la enumeración de cédulas.
MAX_INTENTOS_IDENTIFICACION = 3

ETAPA_PERFILAMIENTO = "perfilamiento"
ETAPA_RESPUESTA = "respuesta"

# Mensajes sin datos de perfil ("hola", "buenas tardes", solo cédula y teléfono):
# no se llama al LLM de extracción. "socio"/"cliente" no están: sí son un dato.
PALABRAS_SIN_DATOS = frozenset("""
    hola buenas buenos buen dia dias tardes noches saludos gracias ok si no
    mi mis es son el la los de del y con por favor numero cedula whatsapp telefono celular
    aqui va le paso esta este""".split())
MARCADORES_PII = re.compile(r"\[[A-Z_]+\]")


def sin_datos_de_perfil(texto_sanitizado: str) -> bool:
    texto = MARCADORES_PII.sub(" ", texto_sanitizado.lower())
    texto = texto.translate(str.maketrans("áéíóúü", "aeiouu"))
    palabras = re.findall(r"[a-zñ]+", texto)
    return "?" not in texto and all(p in PALABRAS_SIN_DATOS for p in palabras)


# =====================================================================
# Extracción con LLM
# =====================================================================
class CultivoExtraido(BaseModel):
    model_config = ConfigDict(extra="ignore")
    nombre: str
    area: Optional[float] = None
    unidad: Optional[str] = None


class ExtraccionPerfil(BaseModel):
    """Salida JSON de perfil_extraccion_prompt. Todo opcional: nunca inventar."""
    model_config = ConfigDict(extra="ignore")

    tipo_usuario: Optional[str] = None
    cultivos: List[CultivoExtraido] = Field(default_factory=list)
    municipio: Optional[str] = None
    departamento: Optional[str] = None
    etapa_cultivo: Optional[str] = None
    topografia: Optional[str] = None
    tipo_suelo: Optional[str] = None
    altura_msnm: Optional[float] = None
    tipo_negocio: Optional[str] = None
    desconoce_terreno: bool = False
    consulta_tecnica: Optional[str] = None

    @field_validator("tipo_usuario")
    @classmethod
    def _tipo_valido(cls, v: Optional[str]) -> Optional[str]:
        v = (v or "").strip().lower().replace("é", "e")
        return v if v in TIPOS_USUARIO else None

    @field_validator("cultivos", mode="before")
    @classmethod
    def _cultivos_lista(cls, v):
        return [c for c in (v or []) if isinstance(c, dict) and str(c.get("nombre") or "").strip()]

    @field_validator("desconoce_terreno", mode="before")
    @classmethod
    def _bool(cls, v):
        return bool(v)


class ExtractorPerfil:
    """Llama al LLM con perfil_extraccion_prompt y valida el JSON. Nunca lanza."""

    def __init__(self, llm_client: AsyncOpenAI) -> None:
        self.llm = llm_client

    async def extraer(self, texto_sanitizado: str, cfg: PromptConfig) -> ExtraccionPerfil:
        try:
            response = await self.llm.chat.completions.create(
                model=cfg.llm_modelo,
                messages=[
                    {"role": "system", "content": cfg.perfil_extraccion_prompt},
                    {"role": "user", "content": texto_sanitizado},
                ],
                temperature=0,
                max_tokens=400,
                response_format={"type": "json_object"},
                timeout=settings.llm_timeout_seconds,
            )
            return ExtraccionPerfil.model_validate(json.loads(response.choices[0].message.content))
        except Exception as exc:
            # Sin extracción el flujo sigue: se vuelve a preguntar lo que falte.
            logger.warning(f"Extracción de perfil fallida ({type(exc).__name__}): {exc}")
            return ExtraccionPerfil()


def _seguro(valor: Optional[str]) -> Optional[str]:
    """
    Campo del contexto sin cédulas ni teléfonos (va al perfil y al prompt de generación).
    Sin el detector de nombres de sanitizar_pii: en un campo suelto marca lugares
    como personas ("Sébaco" -> [NOMBRE]); nombres de persona no se piden en el contexto.
    """
    if not valor or not valor.strip():
        return None
    return ocultar_telefonos(REGEX_CEDULA.sub("[CEDULA]", valor)).strip() or None


def extraccion_de_contexto(ctx: ContextoInicial, consulta_sanitizada: str) -> ExtraccionPerfil:
    """El equivalente de la salida del LLM de extracción, a partir del perfil ya estructurado."""
    municipio = _seguro(ctx.municipio)
    comunidad = _seguro(ctx.comunidad)
    if comunidad:
        municipio = f"{municipio} (comunidad {comunidad})" if municipio else f"comunidad {comunidad}"
    return ExtraccionPerfil(
        tipo_usuario=ctx.tipo_usuario,
        cultivos=[{"nombre": _seguro(c.nombre), "area": c.area, "unidad": c.unidad} for c in ctx.cultivos],
        municipio=municipio,
        departamento=_seguro(ctx.departamento),
        etapa_cultivo=_seguro(ctx.etapa_cultivo),
        topografia=_seguro(ctx.topografia),
        tipo_suelo=_seguro(ctx.tipo_suelo),
        altura_msnm=ctx.altura_msnm,
        tipo_negocio=_seguro(ctx.tipo_negocio),
        consulta_tecnica=consulta_sanitizada or None,
    )


def aplicar_extraccion(perfil: PerfilUsuario, ext: ExtraccionPerfil) -> PerfilUsuario:
    """Completa el perfil con lo declarado. No pisa datos del registro ni lo ya respondido."""
    p = perfil.model_copy(deep=True)
    if ext.tipo_usuario and not p.verificado:
        p.tipo_usuario = ext.tipo_usuario
    if ext.cultivos and not p.cultivos:
        p.cultivos = [Cultivo(nombre=c.nombre.strip(), area_mz=area_a_mz(c.area, c.unidad)) for c in ext.cultivos]
    for campo in ("municipio", "departamento", "etapa_cultivo", "topografia", "tipo_suelo", "tipo_negocio"):
        valor = getattr(ext, campo)
        if valor and str(valor).strip() and not getattr(p, campo):
            setattr(p, campo, str(valor).strip())
    if ext.altura_msnm and ext.altura_msnm > 0 and not p.altura_msnm:
        p.altura_msnm = ext.altura_msnm
    # desconoce_terreno no se usa: el LLM lo marca en mensajes que no hablan del
    # terreno (medido: 2 de 3 en "¿qué le aplico a la roya?") y saltaba la pregunta.
    # La pregunta de terreno ya se hace una sola vez (terreno_preguntado al preguntarla).
    return p


def combinar_con_registro(registro: PerfilUsuario, declarado: PerfilUsuario) -> PerfilUsuario:
    """El registro manda; lo declarado solo llena huecos (p. ej. la etapa del cultivo)."""
    p = registro.model_copy(deep=True)
    if not p.cultivos:
        p.cultivos = declarado.cultivos
    for campo in ("municipio", "departamento", "etapa_cultivo", "topografia", "tipo_suelo",
                  "altura_msnm", "tipo_negocio"):
        if not getattr(p, campo) and getattr(declarado, campo):
            setattr(p, campo, getattr(declarado, campo))
    p.terreno_preguntado = declarado.terreno_preguntado
    return p


# =====================================================================
# Orquestación por turno
# =====================================================================
@dataclass
class ResultadoTurno:
    etapa: str                          # 'perfilamiento' | 'respuesta'
    perfil: PerfilUsuario
    respuesta: Optional[str] = None     # etapa 'perfilamiento': lo que se devuelve
    consulta_para_rag: Optional[str] = None  # etapa 'respuesta': lo que responde el RAG
    aviso: Optional[str] = None         # etapa 'respuesta': texto a anteponer (p. ej. registro encontrado)


def nueva_sesion_id() -> str:
    return uuid.uuid4().hex


class Perfilador:
    def __init__(self, db: AsyncSession, extractor: ExtractorPerfil, salt: Optional[str]) -> None:
        self.db = db
        self.extractor = extractor
        self.salt = salt

    @staticmethod
    def _pregunta_faltantes(datos: List[str], cfg: PromptConfig) -> str:
        """Todo lo que falta en un solo mensaje."""
        textos = [{
            "identificacion": cfg.perfil_msg_pedir_identificacion,
            "cultivo": cfg.perfil_msg_pedir_cultivo,
            "ubicacion": cfg.perfil_msg_pedir_ubicacion,
            "terreno": cfg.perfil_msg_pedir_terreno,
            "consulta": cfg.perfil_msg_pedir_consulta,
        }[d] for d in datos]
        if len(textos) == 1:
            return textos[0]
        return cfg.perfil_msg_pedir_faltantes.format(preguntas="\n".join(f"- {t}" for t in textos))

    async def _identificar(self, sesion: SesionPerfil, perfil: PerfilUsuario, consulta_raw: str,
                           cfg: PromptConfig) -> tuple[PerfilUsuario, Optional[str], bool]:
        """
        Devuelve (perfil, mensaje, pedir_telefono). Lee cédula y teléfono del texto
        CRUDO y los hashea enseguida: ni el dato ni el hash del teléfono se guardan.
        """
        cedulas = extraer_cedula(consulta_raw)
        telefonos = extraer_telefonos(consulta_raw)
        if perfil.verificado or not (cedulas or sesion.cedula_hashes):
            return perfil, None, False
        if not self.salt:
            logger.warning("SEMILLERO_HASH_SALT no configurado: no se puede identificar socios por cédula.")
            return perfil, cfg.perfil_msg_no_encontrado, False
        if sesion.intentos_identificacion >= MAX_INTENTOS_IDENTIFICACION:
            return perfil, cfg.perfil_msg_no_encontrado, False

        if cedulas:
            sesion.cedula_hashes = hashes(cedulas, self.salt)
        if not telefonos:
            return perfil, None, True

        sesion.intentos_identificacion += 1
        with paso("Identificación del socio (perfil registrado)", "postgres"):
            persona_id = await buscar_persona_id(self.db, list(sesion.cedula_hashes), hashes(telefonos, self.salt))
            sesion.cedula_hashes = []
            registro = await cargar_perfil_registrado(self.db, persona_id) if persona_id else None
        if registro is None:
            return perfil, cfg.perfil_msg_no_encontrado, False

        sesion.persona_id = persona_id
        perfil = combinar_con_registro(registro, perfil)
        return perfil, cfg.perfil_msg_verificado.format(resumen=registro.resumen_corto()), False

    async def procesar_turno(self, sesion_id: str, consulta_raw: str, cfg: PromptConfig,
                             contexto: Optional[ContextoInicial] = None) -> ResultadoTurno:
        with paso("Sesión de la conversación (leer)", "postgres"):
            sesion = await self.db.get(SesionPerfil, sesion_id)
        nueva = sesion is None
        if nueva:
            sesion = SesionPerfil(sesion_id=sesion_id, estado="perfilando", perfil={}, cedula_hashes=[],
                                  intentos_identificacion=0, preguntas_hechas=0, preguntas=[])
            self.db.add(sesion)
        perfil = PerfilUsuario.model_validate(sesion.perfil or {})

        if sesion.estado == "completo":
            return ResultadoTurno(ETAPA_RESPUESTA, perfil, consulta_para_rag=consulta_raw)

        # 1. Identificación de socio/cliente (texto crudo -> hashes)
        texto_identificacion = " ".join(x for x in (
            consulta_raw, contexto and contexto.cedula, contexto and contexto.telefono) if x)
        perfil, mensaje, pedir_telefono = await self._identificar(sesion, perfil, texto_identificacion, cfg)

        # 2. Extracción de lo declarado (texto sanitizado: Ley 787). Con contexto, sin LLM.
        texto_seguro = ocultar_telefonos(sanitizar_pii(consulta_raw)[0])
        if contexto is not None:
            ext = extraccion_de_contexto(contexto, texto_seguro.strip())
        elif sin_datos_de_perfil(texto_seguro):
            ext = ExtraccionPerfil()
        else:
            with paso("Extracción del perfil", "llm"):
                ext = await self.extractor.extraer(texto_seguro, cfg)
        perfil = aplicar_extraccion(perfil, ext)
        if ext.consulta_tecnica and ext.consulta_tecnica.strip():
            sesion.consulta_pendiente = ocultar_telefonos(sanitizar_pii(ext.consulta_tecnica.strip())[0])

        # 3. Próxima pregunta
        preguntas = list(sesion.preguntas or [])
        pregunta: Optional[str] = None
        if pedir_telefono:
            pregunta = cfg.perfil_msg_pedir_telefono
        elif sesion.preguntas_hechas < cfg.perfil_max_preguntas:
            if nueva and not perfil.verificado:
                if perfil.tipo_usuario and not self._faltantes(perfil, sesion, ["terreno"]):
                    # El primer mensaje ya dice quién es y trae el caso (p. ej. el formulario
                    # inicial): vale como respuesta a la bienvenida. El terreno es opcional.
                    datos = []
                    perfil.terreno_preguntado = True
                else:
                    # Se empieza con la bienvenida, que ya pide todo el caso (salvo socio identificado).
                    datos = ["bienvenida"]
                    pregunta = cfg.perfil_msg_bienvenida
            else:
                datos = self._faltantes(perfil, sesion, preguntas)
                if datos:
                    pregunta = self._pregunta_faltantes(datos, cfg)
            if pregunta is not None:
                preguntas.extend(datos)
                if "terreno" in datos:
                    perfil.terreno_preguntado = True
        if pregunta is not None:
            sesion.preguntas = preguntas
            sesion.preguntas_hechas += 1
            sesion.perfil = perfil.model_dump(mode="json")
            return ResultadoTurno(ETAPA_PERFILAMIENTO, perfil, respuesta=_unir(mensaje, pregunta))

        # 4. Perfil completo (o sin más preguntas disponibles)
        if not perfil.tipo_usuario:
            perfil.tipo_usuario = "otro"
        sesion.estado = "completo"
        sesion.perfil = perfil.model_dump(mode="json")
        consulta = sesion.consulta_pendiente
        sesion.consulta_pendiente = None
        if consulta:
            return ResultadoTurno(ETAPA_RESPUESTA, perfil, consulta_para_rag=consulta, aviso=mensaje)
        return ResultadoTurno(ETAPA_PERFILAMIENTO, perfil, respuesta=_unir(mensaje, cfg.perfil_msg_completo))

    @staticmethod
    def _faltantes(perfil: PerfilUsuario, sesion: SesionPerfil, ya_preguntados: List[str]) -> List[str]:
        """Todo lo que falta para analizar el caso, sin repetir lo que ya se pidió."""
        sin_consulta = not sesion.consulta_pendiente
        if (perfil.tipo_usuario in ("socio", "cliente") and not perfil.verificado
                and not sesion.cedula_hashes and sesion.intentos_identificacion == 0):
            # Dice ser socio/cliente pero no dio la cédula: el registro trae cultivos y
            # finca, así que solo se pide la identificación (y la consulta).
            candidatos = ["identificacion"] + (["consulta"] if sin_consulta else [])
        else:
            candidatos = [d for d, falta in (
                ("cultivo", not perfil.cultivos and perfil.tipo_usuario not in TIPOS_SIN_FINCA),
                ("ubicacion", not perfil.tiene_ubicacion()),
                ("terreno", perfil.necesita_terreno()),
                ("consulta", sin_consulta),
            ) if falta]
        return [d for d in candidatos if d not in ya_preguntados]


def _unir(*partes: Optional[str]) -> str:
    return "\n\n".join(p for p in partes if p)
