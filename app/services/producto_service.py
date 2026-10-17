# app/services/producto_service.py
"""
Cierre de cada respuesta con un producto de AldeaZON ("Producto AldeaZON recomendado: ...").

Por qué no lo escribe el LLM de generación: con la regla en el system_prompt
(scripts/006-008) gpt-4o-mini fue inestable (medido el 2026-10-17, corpus v2):
recomendó un HERBICIDA para mosca blanca, y con la regla endurecida dejó de
recomendar EVITO T para roya teniéndolo en el contexto, o nombró un producto en
el texto y otro en la línea final.

Ahora:
  1. elegir_producto: una llamada aparte (temperature 0, JSON) elige, entre las
     fichas recuperadas, la que sirve para el MISMO problema y cultivo, o ninguna
     (producto_selector_prompt). Corre en paralelo con la generación.
  2. linea_producto: la línea la arma el código con los datos de la ficha
     (producto, uso, dosis literal de la ficha); sin ficha que sirva, el mensaje
     de la tienda (msg_producto_tienda). Las dos plantillas viven en prompt_config.
  3. cerrar_con_producto: quita cualquier línea de producto que haya escrito el
     LLM y pone la del sistema antes de la fuente.
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.services.prompt_config import PromptConfig

logger = logging.getLogger("atv.producto")

PREFIJO_LINEA = "Producto AldeaZON recomendado:"
REGEX_LINEA_LLM = re.compile(r"^\s*\**\s*Producto AldeaZON recomendado\s*:?.*$", re.IGNORECASE | re.MULTILINE)
REGEX_FUENTE = re.compile(r"\[\s*Fuente\s*:", re.IGNORECASE)
# extractor_ficha guarda "Dosis: <dosis>. <condiciones>"; la dosis termina en un punto seguido de
# mayúscula (la frase siguiente) o del final: así no corta en abreviaturas ("2 ml por Lt. de agua").
REGEX_DOSIS_EN_CONDICIONES = re.compile(r"^Dosis:\s*(.+?)\.(?=\s+[A-ZÁÉÍÓÚÑ¿(]|\s*$)")
MAX_CONDICIONES_SELECTOR = 160


def _texto(valor: Any) -> Optional[str]:
    texto = str(valor).strip() if valor is not None else ""
    return texto if texto and texto.lower() not in {"none", "null"} else None


def _mensaje_selector(consulta: str, cultivo: Optional[str], fichas: List[Dict[str, Any]]) -> str:
    lineas = [f"CONSULTA: {consulta}", f"CULTIVO DEL CLIENTE: {cultivo or 'no se sabe'}", "FICHAS:"]
    for i, f in enumerate(fichas):
        condiciones = (_texto(f.get("condiciones")) or "")[:MAX_CONDICIONES_SELECTOR]
        lineas.append(
            f"{i}. {f.get('producto')} | ingrediente activo: {_texto(f.get('principio_activo')) or '-'} | "
            f"cultivo: {_texto(f.get('cultivo')) or '-'} | "
            f"uso: {_texto(f.get('objetivo')) or '-'} | dosis: {_texto(f.get('dosis')) or '-'} | {condiciones}"
        )
    return "\n".join(lineas)


async def elegir_producto(
    llm_client, cfg: PromptConfig, consulta: str, cultivo: Optional[str], fichas: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """La ficha del producto a recomendar, o None. Falla cerrado (None → mensaje de la tienda)."""
    fichas = [f for f in fichas if _texto(f.get("producto"))]
    if not fichas:
        return None
    try:
        response = await llm_client.chat.completions.create(
            model=cfg.llm_modelo,
            messages=[
                {"role": "system", "content": cfg.producto_selector_prompt},
                {"role": "user", "content": _mensaje_selector(consulta, cultivo, fichas)},
            ],
            temperature=0,
            max_tokens=20,
            response_format={"type": "json_object"},
            timeout=settings.llm_timeout_seconds,
        )
        indice = json.loads(response.choices[0].message.content).get("indice")
    except Exception as exc:
        logger.warning(f"Product selector failed closed ({type(exc).__name__}): {exc}")
        return None
    # Solo vale un índice de la lista: el LLM no puede inventar un producto.
    if isinstance(indice, bool) or not isinstance(indice, int) or not 0 <= indice < len(fichas):
        return None
    return fichas[indice]


def linea_producto(ficha: Optional[Dict[str, Any]], cfg: PromptConfig) -> str:
    if ficha is None:
        return cfg.msg_producto_tienda
    objetivo, cultivo = _texto(ficha.get("objetivo")), _texto(ficha.get("cultivo"))
    if objetivo and objetivo.lower().startswith("ficha general"):
        objetivo = None
    uso = " en ".join(p for p in (objetivo, cultivo) if p) or "según su ficha técnica"
    dosis = _texto(ficha.get("dosis"))
    if not dosis and (m := REGEX_DOSIS_EN_CONDICIONES.match(_texto(ficha.get("condiciones")) or "")):
        dosis = m.group(1)
    return cfg.producto_linea_template.format(
        producto=_texto(ficha.get("producto")),
        uso=uso,
        dosis=f" Dosis según la ficha: {dosis}." if dosis else "",
        documento=_texto(ficha.get("documento_fuente")) or "ficha técnica",
    )


def cerrar_con_producto(respuesta: str, linea: str) -> str:
    """Quita las líneas de producto que haya escrito el LLM y pone la del sistema antes de la fuente."""
    texto = re.sub(r"\n{3,}", "\n\n", REGEX_LINEA_LLM.sub("", respuesta)).strip()
    fuentes = list(REGEX_FUENTE.finditer(texto))
    if not fuentes:
        return f"{texto}\n\n{linea}"
    corte = fuentes[-1].start()
    return f"{texto[:corte].rstrip()}\n\n{linea}\n\n{texto[corte:].lstrip()}"
