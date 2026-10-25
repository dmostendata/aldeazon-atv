# app/services/consumo_llm.py
"""
Consumo de LLM de un request: suma los tokens de TODAS las llamadas al LLM del
turno (extracción de perfil, filtro de alcance, juez de FAQ, selector de
producto, generación), no solo la generación que se guarda en atv.consulta.

    consumo = iniciar_consumo()          # al empezar el request
    ...                                  # llamadas con un cliente medir_cliente(...)
    consumo.resumen()                    # {"llamadas", "tokens_entrada", ...}

Usa un ContextVar: las tareas de asyncio.gather heredan el mismo objeto, así
que las llamadas en paralelo también se cuentan.
"""
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, Optional

# gpt-4o-mini (llm_modelo), USD por token. Los mismos precios que atv.consulta.costo_estimado_usd.
PRECIO_ENTRADA_USD = 0.15 / 1_000_000
PRECIO_SALIDA_USD = 0.60 / 1_000_000


@dataclass
class ConsumoLLM:
    llamadas: int = 0
    tokens_entrada: int = 0
    tokens_salida: int = 0

    def registrar(self, usage: Any) -> None:
        self.llamadas += 1
        if usage is not None:
            self.tokens_entrada += getattr(usage, "prompt_tokens", 0) or 0
            self.tokens_salida += getattr(usage, "completion_tokens", 0) or 0

    @property
    def costo_usd(self) -> float:
        return self.tokens_entrada * PRECIO_ENTRADA_USD + self.tokens_salida * PRECIO_SALIDA_USD

    def resumen(self) -> Dict[str, Any]:
        return {"llamadas": self.llamadas, "tokens_entrada": self.tokens_entrada,
                "tokens_salida": self.tokens_salida, "costo_usd": round(self.costo_usd, 6)}


_consumo: ContextVar[Optional[ConsumoLLM]] = ContextVar("consumo_llm", default=None)


def iniciar_consumo() -> ConsumoLLM:
    consumo = ConsumoLLM()
    _consumo.set(consumo)
    return consumo


def medir_cliente(cliente):
    """Envuelve chat.completions.create del cliente para sumar su usage al consumo del request."""
    original = cliente.chat.completions.create

    async def create(*args, **kwargs):
        respuesta = await original(*args, **kwargs)
        consumo = _consumo.get()
        if consumo is not None:
            consumo.registrar(getattr(respuesta, "usage", None))
        return respuesta

    cliente.chat.completions.create = create
    return cliente
