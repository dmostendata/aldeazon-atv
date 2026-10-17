# app/services/prompt_config.py
"""
Carga de la configuración de prompts del RAG desde PostgreSQL (atv.prompt_config).

La BD es la única fuente de verdad: no hay valores por defecto en código.
Si la BD no responde, falta una clave activa, un valor numérico es inválido o una
plantilla usa placeholders incorrectos, se lanza PromptConfigError y la consulta falla.
"""
import string
from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, Set

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.traceability import PromptConfig as PromptConfigRow


class PromptConfigError(Exception):
    """La configuración de prompts no está disponible o es inválida."""


@dataclass(frozen=True)
class PromptConfig:
    system_prompt: str
    user_prompt_template: str
    chunk_template: str
    msg_retrieval_error: str
    msg_baja_confianza: str
    msg_llm_error: str
    msg_groundedness_rechazo: str
    llm_modelo: str
    llm_temperature: float
    llm_max_tokens: int
    retrieval_top_k: int
    perfil_msg_bienvenida: str
    perfil_msg_pedir_telefono: str
    perfil_msg_no_encontrado: str
    perfil_msg_verificado: str
    perfil_msg_pedir_cultivo: str
    perfil_msg_pedir_ubicacion: str
    perfil_msg_pedir_terreno: str
    perfil_msg_completo: str
    perfil_extraccion_prompt: str
    perfil_max_preguntas: int
    alcance_prompt: str
    msg_fuera_alcance: str


# Conversor esperado por clave. El tipo lo define el código (no la columna `tipo`),
# así un error de edición en la BD no puede cambiar el tipo que recibe rag_service.
_CONVERSORES: Dict[str, Callable[[str], object]] = {
    "system_prompt": str,
    "user_prompt_template": str,
    "chunk_template": str,
    "msg_retrieval_error": str,
    "msg_baja_confianza": str,
    "msg_llm_error": str,
    "msg_groundedness_rechazo": str,
    "llm_modelo": str,
    "llm_temperature": float,
    "llm_max_tokens": int,
    "retrieval_top_k": int,
    "perfil_msg_bienvenida": str,
    "perfil_msg_pedir_telefono": str,
    "perfil_msg_no_encontrado": str,
    "perfil_msg_verificado": str,
    "perfil_msg_pedir_cultivo": str,
    "perfil_msg_pedir_ubicacion": str,
    "perfil_msg_pedir_terreno": str,
    "perfil_msg_completo": str,
    "perfil_extraccion_prompt": str,
    "perfil_max_preguntas": int,
    "alcance_prompt": str,
    "msg_fuera_alcance": str,
}

# Placeholders que deben aparecer obligatoriamente en cada plantilla.
_PLACEHOLDERS_REQUERIDOS: Dict[str, FrozenSet[str]] = {
    "user_prompt_template": frozenset({"contexto", "consulta", "perfil"}),
    "chunk_template": frozenset(),
    "perfil_msg_verificado": frozenset({"resumen"}),
}

# Placeholders permitidos: cualquier otro provocaría KeyError en .format().
_PLACEHOLDERS_PERMITIDOS: Dict[str, FrozenSet[str]] = {
    "user_prompt_template": frozenset({"contexto", "consulta", "perfil"}),
    "chunk_template": frozenset({"documento_fuente", "producto", "cultivo", "dosis", "condiciones"}),
    "perfil_msg_verificado": frozenset({"resumen"}),
}

CAMPOS_CHUNK = tuple(sorted(_PLACEHOLDERS_PERMITIDOS["chunk_template"]))


def _placeholders(plantilla: str) -> Set[str]:
    try:
        return {campo for _, campo, _, _ in string.Formatter().parse(plantilla) if campo is not None}
    except ValueError as exc:
        raise PromptConfigError(f"Plantilla con sintaxis inválida: {exc}") from exc


def _validar_plantilla(clave: str, plantilla: str) -> None:
    usados = _placeholders(plantilla)
    faltantes = _PLACEHOLDERS_REQUERIDOS[clave] - usados
    if faltantes:
        raise PromptConfigError(f"'{clave}' no contiene los placeholders requeridos: {sorted(faltantes)}")
    desconocidos = usados - _PLACEHOLDERS_PERMITIDOS[clave]
    if desconocidos:
        raise PromptConfigError(
            f"'{clave}' usa placeholders no permitidos: {sorted(desconocidos)}. "
            f"Permitidos: {sorted(_PLACEHOLDERS_PERMITIDOS[clave])}"
        )


def construir_prompt_config(filas: Dict[str, str]) -> PromptConfig:
    """Valida y convierte un dict clave->valor (texto crudo de la BD) en PromptConfig."""
    faltantes = sorted(set(_CONVERSORES) - set(filas))
    if faltantes:
        raise PromptConfigError(f"Faltan claves activas en atv.prompt_config: {faltantes}")

    valores: Dict[str, object] = {}
    for clave, conversor in _CONVERSORES.items():
        crudo = filas[clave]
        try:
            valor = conversor(crudo.strip() if conversor is not str else crudo)
        except (TypeError, ValueError) as exc:
            raise PromptConfigError(
                f"Valor inválido para '{clave}' ({conversor.__name__}): {crudo!r}"
            ) from exc
        if conversor is str and not valor.strip():
            raise PromptConfigError(f"'{clave}' está vacío en atv.prompt_config")
        valores[clave] = valor

    for clave in _PLACEHOLDERS_PERMITIDOS:
        _validar_plantilla(clave, valores[clave])

    if valores["llm_max_tokens"] <= 0 or valores["retrieval_top_k"] <= 0:
        raise PromptConfigError("'llm_max_tokens' y 'retrieval_top_k' deben ser mayores que 0")
    if valores["perfil_max_preguntas"] < 0:
        raise PromptConfigError("'perfil_max_preguntas' no puede ser negativo")

    return PromptConfig(**valores)


async def cargar_prompt_config(session: AsyncSession) -> PromptConfig:
    """Lee las claves activas de atv.prompt_config (una sola query) y las valida."""
    try:
        resultado = await session.execute(
            select(PromptConfigRow.clave, PromptConfigRow.valor).where(PromptConfigRow.activo.is_(True))
        )
        filas = {clave: valor for clave, valor in resultado.all()}
    except SQLAlchemyError as exc:
        raise PromptConfigError(f"No se pudo leer atv.prompt_config: {exc}") from exc

    return construir_prompt_config(filas)
