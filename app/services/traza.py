# app/services/traza.py
"""
Latencia por paso de un request, para encontrar cuellos de botella: cada paso
del flujo (Postgres, Qdrant, embeddings, cada llamada al LLM, Redis, CPU) con su
inicio y su duración, relativos al comienzo del request.

    traza = iniciar_traza()                         # al empezar el request
    with paso("Búsqueda en Qdrant", "qdrant"):      # código síncrono o con await adentro
        ...
    await medido("Juez de FAQ", "llm", coro)        # para corrutinas de un asyncio.gather
    traza.resumen()

Como consumo_llm.py, usa un ContextVar: los pasos que corren en paralelo dentro
de asyncio.gather se registran en la misma traza (se solapan en el tiempo).
Fuera de un request (tests, scripts) no registra nada.
"""
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Awaitable, Dict, List, Optional, TypeVar

T = TypeVar("T")

# Componentes: dónde se va el tiempo.
COMPONENTES = ("postgres", "qdrant", "embedding", "llm", "redis", "cpu")


@dataclass
class Traza:
    t0: float = field(default_factory=time.perf_counter)
    pasos: List[Dict[str, Any]] = field(default_factory=list)

    def registrar(self, nombre: str, componente: str, inicio: float, fin: float) -> None:
        self.pasos.append({"paso": nombre, "componente": componente,
                           "inicio_ms": round((inicio - self.t0) * 1000, 1),
                           "ms": round((fin - inicio) * 1000, 1)})

    def resumen(self) -> Dict[str, Any]:
        por_componente: Dict[str, float] = {}
        for p in self.pasos:
            por_componente[p["componente"]] = round(por_componente.get(p["componente"], 0) + p["ms"], 1)
        return {"total_ms": round((time.perf_counter() - self.t0) * 1000, 1),
                "pasos": sorted(self.pasos, key=lambda p: p["inicio_ms"]),
                "por_componente": por_componente}


_traza: ContextVar[Optional[Traza]] = ContextVar("traza_latencia", default=None)


def iniciar_traza() -> Traza:
    traza = Traza()
    _traza.set(traza)
    return traza


@contextmanager
def paso(nombre: str, componente: str):
    traza = _traza.get()
    inicio = time.perf_counter()
    try:
        yield
    finally:
        if traza is not None:
            traza.registrar(nombre, componente, inicio, time.perf_counter())


async def medido(nombre: str, componente: str, coro: Awaitable[T]) -> T:
    with paso(nombre, componente):
        return await coro
