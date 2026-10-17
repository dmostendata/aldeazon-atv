# app/services/faq_service.py
"""
Preguntas frecuentes aprobadas por Asistencia Técnica (atv.pregunta_frecuente).

Es el punto de partida de cada consulta: antes de buscar en las fichas técnicas
se revisa si el cliente pregunta algo que ya está en la lista.

Dos pasos:
  1. Candidatas: similitud coseno (multilingual-e5-large, el mismo modelo que ya
     tiene cargado la API) entre la consulta y la pregunta + variantes de cada
     fila. Solo ordena; no decide.
  2. Juez: un LLM decide si la consulta pregunta LO MISMO que alguna candidata
     (faq_juez_prompt). Un umbral de similitud fijo no sirve: "¿qué tiene para
     roya?" y "¿cuánto Opera echo por manzana para roya?" se parecen mucho por
     embedding, pero la segunda pide una dosis que la respuesta aprobada no da.

La tabla es la fuente de verdad: los embeddings se recalculan en memoria
cuando cambia (huella = filas activas + último ts_actualizado), así una recarga
del Excel entra sin reiniciar la API.
"""
import json
import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.traceability import PreguntaFrecuente as PreguntaFrecuenteRow
from app.services.prompt_config import PromptConfig

logger = logging.getLogger("atv.faq")

# e5: "query: " en ambos lados para comparar pregunta contra pregunta (tarea simétrica).
PREFIJO_E5 = "query: "


@dataclass(frozen=True)
class PreguntaFrecuente:
    codigo: str
    cultivo: Optional[str]
    pregunta: str
    variantes: Tuple[str, ...]
    respuesta: str
    respuesta_evitar: Optional[str]


@dataclass(frozen=True)
class Candidata:
    faq: PreguntaFrecuente
    similitud: float


def _normalizar(matriz: np.ndarray) -> np.ndarray:
    normas = np.linalg.norm(matriz, axis=-1, keepdims=True)
    return matriz / np.where(normas == 0, 1, normas)


class IndiceFAQ:
    """Embeddings en memoria de atv.pregunta_frecuente. Uno por proceso (indice_faq)."""

    def __init__(self) -> None:
        self._huella: Optional[tuple] = None
        self._faqs: List[PreguntaFrecuente] = []
        self._matriz: Optional[np.ndarray] = None   # (n_textos, dim), normalizada
        self._dueno: Optional[np.ndarray] = None    # índice en _faqs de cada texto

    async def sincronizar(self, session: AsyncSession, embedding_model) -> None:
        huella = tuple((await session.execute(
            select(func.count(), func.max(PreguntaFrecuenteRow.ts_actualizado))
            .where(PreguntaFrecuenteRow.activo.is_(True))
        )).one())
        if huella == self._huella:
            return

        filas = (await session.execute(
            select(PreguntaFrecuenteRow).where(PreguntaFrecuenteRow.activo.is_(True))
            .order_by(PreguntaFrecuenteRow.codigo)
        )).scalars().all()
        faqs = [
            PreguntaFrecuente(
                codigo=f.codigo, cultivo=f.cultivo, pregunta=f.pregunta,
                variantes=tuple(f.variantes or ()), respuesta=f.respuesta,
                respuesta_evitar=f.respuesta_evitar,
            )
            for f in filas
        ]
        textos, dueno = [], []
        for i, faq in enumerate(faqs):
            for texto in (faq.pregunta, *faq.variantes):
                textos.append(PREFIJO_E5 + texto)
                dueno.append(i)

        self._faqs = faqs
        self._matriz = _normalizar(np.array(list(embedding_model.embed(textos)))) if textos else None
        self._dueno = np.array(dueno)
        self._huella = huella
        logger.info(f"FAQ index rebuilt: {len(faqs)} preguntas, {len(textos)} textos")

    def candidatas(self, embedding_model, consulta: str, n: int) -> List[Candidata]:
        """Las n preguntas más parecidas (por su texto más parecido: pregunta o variante)."""
        if self._matriz is None or n <= 0:
            return []
        vector = _normalizar(np.array(list(embedding_model.embed([PREFIJO_E5 + consulta]))[0]))
        similitudes = self._matriz @ vector
        mejor = np.full(len(self._faqs), -1.0)
        np.maximum.at(mejor, self._dueno, similitudes)
        orden = np.argsort(-mejor)[:n]
        return [Candidata(self._faqs[i], float(mejor[i])) for i in orden]


indice_faq = IndiceFAQ()


def _mensaje_juez(consulta: str, cultivo: Optional[str], candidatas: List[Candidata]) -> str:
    lineas = [f"CONSULTA: {consulta}", f"CULTIVO DEL CLIENTE: {cultivo or 'no se sabe'}", "CANDIDATAS:"]
    for c in candidatas:
        otras = " | ".join(c.faq.variantes)
        lineas.append(
            f"- {c.faq.codigo} [{c.faq.cultivo or 'general'}] {c.faq.pregunta}"
            + (f" (también: {otras})" if otras else "")
        )
    return "\n".join(lineas)


async def elegir_faq(
    llm_client, cfg: PromptConfig, consulta: str, cultivo: Optional[str], candidatas: List[Candidata]
) -> Optional[Candidata]:
    """La candidata que pregunta lo mismo que la consulta, o None. Falla cerrado (None → fichas)."""
    if not candidatas:
        return None
    try:
        response = await llm_client.chat.completions.create(
            model=cfg.llm_modelo,
            messages=[
                {"role": "system", "content": cfg.faq_juez_prompt},
                {"role": "user", "content": _mensaje_juez(consulta, cultivo, candidatas)},
            ],
            temperature=0,
            max_tokens=30,
            response_format={"type": "json_object"},
            timeout=settings.llm_timeout_seconds,
        )
        codigo = json.loads(response.choices[0].message.content).get("codigo")
    except Exception as exc:
        logger.warning(f"FAQ judge failed closed ({type(exc).__name__}): {exc}")
        return None
    # Solo vale un código de las candidatas: el LLM no puede inventar uno.
    return next((c for c in candidatas if c.faq.codigo == codigo), None)
