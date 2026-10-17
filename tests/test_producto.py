# tests/test_producto.py
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.services.producto_service import cerrar_con_producto, elegir_producto, linea_producto
from app.services.prompt_config import construir_prompt_config
from tests.test_prompt_config import FILAS_VALIDAS

CFG = construir_prompt_config(FILAS_VALIDAS)
EVITO = {"producto": "EVITO T", "cultivo": "Café", "objetivo": "Roya", "dosis": "0.3 Lt/Ha",
         "documento_fuente": "FT EVITO T.pdf"}
ROOT = {"producto": "ROOT-PLUS", "cultivo": "Melón", "objetivo": None, "dosis": None,
        "condiciones": "Dosis: 2 ml por Lt. de Agua. Semillero vía drench."}
TOTEM = {"producto": "TOTEM 60 SL", "cultivo": "Café", "objetivo": "malezas", "dosis": "1.5 L/ha"}


def _llm(contenido):
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=contenido))]))
    return client


def test_elegir_producto_por_indice_y_sin_inventar():
    fichas = [TOTEM, EVITO]
    assert asyncio.run(elegir_producto(_llm('{"indice": 1}'), CFG, "roya", "café", fichas)) is EVITO
    assert asyncio.run(elegir_producto(_llm('{"indice": 7}'), CFG, "roya", "café", fichas)) is None
    assert asyncio.run(elegir_producto(_llm('{"indice": null}'), CFG, "roya", "café", fichas)) is None
    assert asyncio.run(elegir_producto(_llm('{"indice": true}'), CFG, "roya", "café", fichas)) is None
    assert asyncio.run(elegir_producto(_llm("no json"), CFG, "roya", "café", fichas)) is None
    assert asyncio.run(elegir_producto(_llm('{"indice": 0}'), CFG, "roya", "café", [])) is None


def test_linea_usa_los_datos_de_la_ficha():
    assert linea_producto(EVITO, CFG) == "Producto AldeaZON recomendado: EVITO T (Roya en Café). Dosis según la ficha: 0.3 Lt/Ha."
    assert "2 ml por Lt. de Agua" in linea_producto(ROOT, CFG)       # dosis guardada en condiciones
    assert linea_producto(None, CFG) == CFG.msg_producto_tienda


def test_cerrar_reemplaza_la_linea_del_llm_y_va_antes_de_la_fuente():
    texto = "Confirme la roya.\n\nProducto AldeaZON recomendado: TOTEM 60 SL.\n\n[Fuente: FAQ-001]"
    cerrado = cerrar_con_producto(texto, "Producto AldeaZON recomendado: EVITO T.")
    assert "TOTEM" not in cerrado
    assert cerrado == "Confirme la roya.\n\nProducto AldeaZON recomendado: EVITO T.\n\n[Fuente: FAQ-001]"
    assert cerrar_con_producto("Sin fuente.", "L.") == "Sin fuente.\n\nL."
