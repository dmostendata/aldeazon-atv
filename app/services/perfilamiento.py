# app/services/perfilamiento.py
"""
Perfilamiento conversacional: antes de recomendar, el ATV pregunta quién es el
usuario y cómo es su finca o negocio.

Flujo por sesión (atv.sesion_perfil):
  1. Sesión nueva -> siempre perfil_msg_bienvenida (¿es socio/cliente? cédula +
     WhatsApp; si no, ¿a qué se dedica?), salvo que el primer mensaje ya lo
     identifique como socio/cliente.
  2. Cédula + teléfono -> se buscan (hasheados) en atv.perfil_productor. Si
     coinciden, el perfil sale del registro: rubros, fincas, lotes, ubicación.
  3. Lo demás lo extrae un LLM (JSON) del mensaje ya sanitizado, y se pregunta
     lo que falte, en orden: cultivo -> ubicación -> etapa/terreno (opcional).
     Cada dato se pregunta una sola vez; con perfil_max_preguntas se responde
     con lo que haya.
  4. Perfil completo -> la consulta técnica que el usuario haya hecho en el
     camino (consulta_pendiente) pasa al RAG con el perfil.
"""
import json
import logging
import uuid
from dataclasses import dataclass
from typing import List, Optional

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.traceability import SesionPerfil
from app.services.perfil_cliente import (
    TIPOS_USUARIO, Cultivo, PerfilUsuario, area_a_mz, buscar_persona_id, cargar_perfil_registrado,
    extraer_cedula, extraer_telefonos, hashes, ocultar_telefonos,
)
from app.services.pii_sanitizer import sanitizar_pii
from app.services.prompt_config import PromptConfig

logger = logging.getLogger("atv.perfilamiento")

# Intentos de cédula + teléfono por sesión: frena la enumeración de cédulas.
MAX_INTENTOS_IDENTIFICACION = 3

ETAPA_PERFILAMIENTO = "perfilamiento"
ETAPA_RESPUESTA = "respuesta"


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

    def _pregunta(self, dato: str, cfg: PromptConfig) -> str:
        return {
            "tipo_usuario": cfg.perfil_msg_bienvenida,
            "cultivo": cfg.perfil_msg_pedir_cultivo,
            "ubicacion": cfg.perfil_msg_pedir_ubicacion,
            "terreno": cfg.perfil_msg_pedir_terreno,
        }[dato]

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
        persona_id = await buscar_persona_id(self.db, list(sesion.cedula_hashes), hashes(telefonos, self.salt))
        sesion.cedula_hashes = []
        registro = await cargar_perfil_registrado(self.db, persona_id) if persona_id else None
        if registro is None:
            return perfil, cfg.perfil_msg_no_encontrado, False

        sesion.persona_id = persona_id
        perfil = combinar_con_registro(registro, perfil)
        return perfil, cfg.perfil_msg_verificado.format(resumen=registro.resumen_corto()), False

    async def procesar_turno(self, sesion_id: str, consulta_raw: str, cfg: PromptConfig) -> ResultadoTurno:
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
        perfil, mensaje, pedir_telefono = await self._identificar(sesion, perfil, consulta_raw, cfg)

        # 2. Extracción de lo declarado (texto sanitizado: Ley 787)
        texto_seguro = ocultar_telefonos(sanitizar_pii(consulta_raw)[0])
        ext = await self.extractor.extraer(texto_seguro, cfg)
        perfil = aplicar_extraccion(perfil, ext)
        if ext.consulta_tecnica and ext.consulta_tecnica.strip():
            sesion.consulta_pendiente = ocultar_telefonos(sanitizar_pii(ext.consulta_tecnica.strip())[0])

        # 3. Próxima pregunta
        preguntas = list(sesion.preguntas or [])
        pregunta: Optional[str] = None
        dato: Optional[str] = None
        if pedir_telefono:
            pregunta = cfg.perfil_msg_pedir_telefono
        elif nueva and not perfil.verificado:
            # Siempre se empieza preguntando quién es (salvo socio ya identificado).
            dato = "tipo_usuario"
        else:
            dato = self._siguiente_dato(perfil, preguntas)
        if pregunta is None and dato and sesion.preguntas_hechas < cfg.perfil_max_preguntas:
            pregunta = self._pregunta(dato, cfg)
            preguntas.append(dato)
            if dato == "terreno":
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
    def _siguiente_dato(perfil: PerfilUsuario, ya_preguntados: List[str]) -> Optional[str]:
        """Próximo dato a preguntar, en orden, sin repetir preguntas que el usuario no contestó."""
        candidatos = []
        if not perfil.tipo_usuario:
            candidatos.append("tipo_usuario")
        if not perfil.cultivos:
            candidatos.append("cultivo")
        if not perfil.tiene_ubicacion():
            candidatos.append("ubicacion")
        if perfil.necesita_terreno():
            candidatos.append("terreno")
        return next((d for d in candidatos if d not in ya_preguntados), None)


def _unir(*partes: Optional[str]) -> str:
    return "\n\n".join(p for p in partes if p)
