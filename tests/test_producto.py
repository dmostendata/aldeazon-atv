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


# ---- Inventario ----------------------------------------------------------------------------
from app.services.producto_service import nombre_corto_producto, resumir_existencias, texto_existencias


def test_nombre_corto_cruza_ficha_e_inventario():
    assert nombre_corto_producto("EVITO T") == nombre_corto_producto("Evito T 450 SC") == "evitot"
    assert nombre_corto_producto("ROOT-PLUS") == nombre_corto_producto("Root Plus 24 SL") == "rootplus"
    # ficha más larga que el inventario: se cruza por "contiene" (SQL_EXISTENCIAS)
    assert nombre_corto_producto("NPK Plus") in nombre_corto_producto("ALDEAZON NPK PLUS+")
    assert nombre_corto_producto("Fluazifop 15EC") in nombre_corto_producto("FLUAZIFOP-P-BUTYL 15 EC")
    assert nombre_corto_producto("Floración Plus") == "floracionplus"


def test_resumir_existencias_ordena_y_agrupa_presentaciones():
    filas = [
        {"bodega_nombre": "Pantasma", "sucursal_id": 3, "unidad": "Litros", "cantidad_disponible": 23},
        {"bodega_nombre": "Jinotega", "sucursal_id": 1, "unidad": "Litros", "cantidad_disponible": 11},
        {"bodega_nombre": "Jinotega", "sucursal_id": 1, "unidad": "Frasco 250 ML", "cantidad_disponible": 40},
        {"bodega_nombre": "Yali", "sucursal_id": 2, "unidad": "Litros", "cantidad_disponible": 7},
        {"bodega_nombre": "Dalia", "sucursal_id": 4, "unidad": "Litros", "cantidad_disponible": 5},
    ]
    assert resumir_existencias(filas, 2, None) == "Jinotega: 40 × Frasco 250 ML + 11 Litros; Pantasma: 23 Litros y 2 tiendas más"
    # la sucursal del cliente va primero aunque tenga menos
    assert resumir_existencias(filas, 1, 4) == "Dalia: 5 Litros y 3 tiendas más"
    assert resumir_existencias(filas[:1], 3, None) == "Pantasma: 23 Litros"


class _Res:
    def __init__(self, escalar=None, filas=()):
        self._escalar, self._filas = escalar, filas

    def scalar(self):
        return self._escalar

    def mappings(self):
        return self._filas


def _sesion(hay_inventario, propias=(), mismas=()):
    sesion = MagicMock()
    sesion.execute = AsyncMock(side_effect=[_Res(hay_inventario), _Res(filas=propias), _Res(filas=mismas)])
    sesion.rollback = AsyncMock()
    return sesion


def _fila(pid, nombre, tienda, cantidad, ingredientes=(), unidad="Litros"):
    from app.services.producto_service import claves_ingrediente
    return {"producto_id": pid, "producto_nombre": nombre, "bodega_nombre": tienda, "sucursal_id": None,
            "unidad": unidad, "cantidad_disponible": cantidad, "ingredientes": list(ingredientes),
            "ingredientes_clave": [" ".join(claves_ingrediente(n)) for n in ingredientes]}


EVITO_FICHA = {**EVITO, "principio_activo": "Fluoxastrobin + Tebuconazole"}


def test_texto_existencias_producto_y_mismo_ingrediente():
    propias = [_fila(2190, "Evito T 450 SC", "Pantasma", 23)]
    mismas = [_fila(5, "Custodia 32 SC", "Jinotega", 300, ["Azoxystrobina", "Tebuconazole"]),
              _fila(5, "Custodia 32 SC", "Pantasma", 200, ["Azoxystrobina", "Tebuconazole"]),
              _fila(7, "Scanner 37 SC", "Yali", 52, ["Tebuconazole", "Prochloraz"]),
              _fila(2190, "Evito T 450 SC", "Pantasma", 23, ["Tebuconazole"])]   # el propio no se repite
    texto = asyncio.run(texto_existencias(_sesion(True, propias, mismas), EVITO_FICHA, CFG))
    assert texto == ("Disponible en: Pantasma: 23 Litros.\nCon Tebuconazole:\n"
                     "- Custodia 32 SC (Tebuconazole): Jinotega: 300 Litros; Pantasma: 200 Litros\n"
                     "- Scanner 37 SC (Tebuconazole): Yali: 52 Litros")


def test_texto_existencias_sin_stock_propio_igual_ofrece_alternativas():
    mismas = [_fila(5, "Custodia 32 SC", "Jinotega", 300, ["Tebuconazole"])]
    texto = asyncio.run(texto_existencias(_sesion(True, [_fila(2190, "Evito T 450 SC", "Pantasma", 0)], mismas), EVITO_FICHA, CFG))
    assert texto.startswith(CFG.msg_sin_existencias) and "Custodia 32 SC" in texto


def test_texto_existencias_casos_limite():
    # inventario aún no cargado: lo dice, sin afirmar que no hay existencias
    assert asyncio.run(texto_existencias(_sesion(False), EVITO_FICHA, CFG)) == CFG.msg_inventario_no_disponible
    assert asyncio.run(texto_existencias(_sesion(True), None, CFG)) is None
    assert asyncio.run(texto_existencias(_sesion(True, [], []), EVITO_FICHA, CFG)) == CFG.msg_sin_existencias


def test_nombre_ingrediente_visible():
    from app.services.producto_service import nombre_ingrediente_visible
    assert nombre_ingrediente_visible("Imidacloprid SC 200 (100+100 g/L)") == "Imidacloprid"
    assert nombre_ingrediente_visible("Lambda Cihalotrina") == "Lambda Cihalotrina"
    assert nombre_ingrediente_visible("Emamectin Benzoato") == "Emamectin Benzoato"


def test_texto_existencias_error_de_base_avisa_no_disponible():
    sesion = MagicMock()
    sesion.execute = AsyncMock(side_effect=RuntimeError("permission denied"))
    sesion.rollback = AsyncMock()
    assert asyncio.run(texto_existencias(sesion, EVITO_FICHA, CFG)) == CFG.msg_inventario_no_disponible
    sesion.rollback.assert_awaited()
