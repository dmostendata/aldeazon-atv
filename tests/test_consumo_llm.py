# tests/test_consumo_llm.py
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.services.consumo_llm import PRECIO_ENTRADA_USD, PRECIO_SALIDA_USD, iniciar_consumo, medir_cliente


def _cliente(*usos):
    cliente = MagicMock()
    cliente.chat.completions.create = AsyncMock(side_effect=[
        SimpleNamespace(usage=u and SimpleNamespace(prompt_tokens=u[0], completion_tokens=u[1])) for u in usos])
    return medir_cliente(cliente)


def test_suma_todas_las_llamadas_del_request_incluso_en_paralelo():
    cliente = _cliente((100, 10), (200, 20), None)

    async def request():
        consumo = iniciar_consumo()
        await asyncio.gather(cliente.chat.completions.create(), cliente.chat.completions.create())
        await cliente.chat.completions.create()   # sin usage: cuenta la llamada, no tokens
        return consumo

    consumo = asyncio.run(request())
    assert consumo.resumen() == {"llamadas": 3, "tokens_entrada": 300, "tokens_salida": 30,
                                 "costo_usd": round(300 * PRECIO_ENTRADA_USD + 30 * PRECIO_SALIDA_USD, 6)}


def test_fuera_de_un_request_no_falla():
    cliente = _cliente((5, 5))
    asyncio.run(cliente.chat.completions.create())  # sin iniciar_consumo: no se registra nada
