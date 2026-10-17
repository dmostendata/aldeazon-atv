# tests/test_prompt_config.py
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from app.services.prompt_config import (
    PromptConfigError,
    cargar_prompt_config,
    construir_prompt_config,
)

FILAS_VALIDAS = {
    "system_prompt": "Eres el ATV. Cierra con [Fuente: {documento}, versión {v}].",
    "user_prompt_template": "CONTEXTO:\n{contexto}\n\nCONSULTA:\n{consulta}\n\nPERFIL:\n{perfil}",
    "chunk_template": "[Documento: {documento_fuente} | Producto: {producto} | Cultivo: {cultivo} | Dosis: {dosis} | Condiciones: {condiciones}]",
    "msg_retrieval_error": "Qdrant caído.",
    "msg_baja_confianza": "Baja confianza.",
    "msg_llm_error": "LLM caído.",
    "msg_groundedness_rechazo": "Dosis rechazada.",
    "llm_modelo": "gpt-4o-mini",
    "llm_temperature": "0.2",
    "llm_max_tokens": "250",
    "retrieval_top_k": " 3 ",
    "perfil_msg_bienvenida": "¿Es socio de Aldea Global?",
    "perfil_msg_pedir_telefono": "¿Su WhatsApp?",
    "perfil_msg_no_encontrado": "No lo encontré.",
    "perfil_msg_verificado": "Encontré su registro: {resumen}.",
    "perfil_msg_pedir_cultivo": "¿Qué cultiva?",
    "perfil_msg_pedir_ubicacion": "¿Dónde?",
    "perfil_msg_pedir_terreno": "¿Cómo es el terreno?",
    "perfil_msg_completo": "¿Cuál es su consulta?",
    "perfil_extraccion_prompt": "Responde JSON.",
    "perfil_max_preguntas": "4",
    "alcance_prompt": "Responde JSON en_alcance.",
    "msg_fuera_alcance": "Solo temas de Aldea.",
}


def _con(**cambios):
    filas = dict(FILAS_VALIDAS)
    for clave, valor in cambios.items():
        if valor is None:
            filas.pop(clave)
        else:
            filas[clave] = valor
    return filas


def test_carga_valida_y_tipos():
    cfg = construir_prompt_config(FILAS_VALIDAS)
    assert cfg.llm_temperature == 0.2
    assert cfg.llm_max_tokens == 250
    assert cfg.retrieval_top_k == 3
    # El system prompt se conserva literal (llaves incluidas, no se formatea)
    assert "{documento}" in cfg.system_prompt


def test_falta_clave():
    with pytest.raises(PromptConfigError, match="system_prompt"):
        construir_prompt_config(_con(system_prompt=None))


def test_int_invalido():
    with pytest.raises(PromptConfigError, match="llm_max_tokens"):
        construir_prompt_config(_con(llm_max_tokens="muchos"))


def test_texto_vacio():
    with pytest.raises(PromptConfigError, match="msg_llm_error"):
        construir_prompt_config(_con(msg_llm_error="   "))


def test_plantilla_sin_placeholder_requerido():
    with pytest.raises(PromptConfigError, match="contexto"):
        construir_prompt_config(_con(user_prompt_template="Solo {consulta}"))


def test_plantilla_con_placeholder_desconocido():
    with pytest.raises(PromptConfigError, match="precio"):
        construir_prompt_config(_con(chunk_template="[{producto} | {precio}]"))


def test_plantilla_sintaxis_invalida():
    with pytest.raises(PromptConfigError):
        construir_prompt_config(_con(user_prompt_template="{contexto} {consulta"))


def test_top_k_cero():
    with pytest.raises(PromptConfigError):
        construir_prompt_config(_con(retrieval_top_k="0"))


def test_cargar_desde_sesion():
    resultado = MagicMock()
    resultado.all.return_value = list(FILAS_VALIDAS.items())
    session = MagicMock()
    session.execute = AsyncMock(return_value=resultado)

    cfg = asyncio.run(cargar_prompt_config(session))
    assert cfg.llm_modelo == "gpt-4o-mini"
    session.execute.assert_awaited_once()


def test_cargar_bd_caida():
    session = MagicMock()
    session.execute = AsyncMock(side_effect=OperationalError("SELECT", {}, Exception("down")))
    with pytest.raises(PromptConfigError, match="No se pudo leer"):
        asyncio.run(cargar_prompt_config(session))


def test_plantilla_sin_perfil():
    with pytest.raises(PromptConfigError, match="perfil"):
        construir_prompt_config(_con(user_prompt_template="{contexto} {consulta}"))


def test_msg_verificado_sin_resumen():
    with pytest.raises(PromptConfigError, match="resumen"):
        construir_prompt_config(_con(perfil_msg_verificado="Encontré su registro."))
