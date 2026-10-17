# app/services/extractor_ficha.py
"""
Extracción por FICHA (corpus v2), en reemplazo de la extracción por línea de extractor.py.

Por qué: extractor.py solo guardaba líneas con un cultivo de una lista fija de 14
Y una dosis "x/ha|mz" en la misma línea. Con eso 74 de 142 fichas quedaron sin
ningún registro (fertilizantes sin dosis por ha, tablas cuyo cultivo y dosis caen
en líneas distintas, dosis por litro/bomba/planta, cultivos fuera de la lista) y
las indexadas quedaron con ~2 fragmentos cada una (medido el 2026-10-17).

Ahora, por cada PDF:
  1. Texto: pdfplumber; si casi no hay texto (escaneado o texto como curvas),
     transcripción por visión (extractor.transcribir_con_vision).
  2. Estructura: UNA llamada al LLM (JSON) que devuelve el producto, su tipo,
     descripción, precauciones y la lista de usos (cultivo, objetivo, dosis,
     condiciones) tal como dice la ficha.
  3. Anclaje: una dosis solo se conserva si cada cifra ("0,7-1,5") aparece en el
     texto de la ficha Y su unidad ("L/ha", "ml") también aparece en la ficha
     (ignorando espacios). La que no se descarta: el LLM no puede meter cifras al
     corpus ni cambiar L por mL. No se exige cifra y unidad juntas porque en las
     tablas la unidad va en el encabezado ("Dosis L/ha" ... "0.7-1.0") y en las
     fichas a dos columnas "2.5 ml por" y "Lt. de agua" salen en líneas distintas.
  4. Registros: uno por uso + uno general de la ficha (para que un fertilizante
     o una semilla sin tabla de dosis también se encuentre).

El campo `dosis` de RegistroAtomico sigue exigiendo unidad de área (/ha, /mz):
las dosis por litro, bomba, planta, etc. van a `condiciones` ("Dosis: ...").
"""
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

import pdfplumber
from openai import OpenAI
from pydantic import ValidationError

from app.core.config import settings
from app.schemas.domain import RegistroAtomico
from app.services.extractor import transcribir_con_vision

logger = logging.getLogger("atv.extractor_ficha")

MODELO = "gpt-4o-mini"
MIN_CARACTERES_TEXTO = 300      # por debajo, el PDF se trata como escaneado
MAX_CARACTERES_LLM = 40000      # las hojas de seguridad más largas rondan 27.000
MAX_CONDICIONES = 700
MAX_GENERAL = 900

# Cifra (o rango), su unidad y, si la trae, el área: lo que se ancla contra la ficha
CIFRA_CON_UNIDAD = re.compile(
    r'(\d[\d.,]*(?:\s*(?:-|a)\s*\d[\d.,]*)?)\s*(kg|gr?|l|lt|lts|litros?|ml|cc|%|qq|lbs?|oz|cm|m)\b(\s*/\s*(?:ha|mz)\b)?',
    re.IGNORECASE,
)
VALORES_NULOS = {"null", "none", "n/a", "na", "-", "—", "no aplica", "no especificado"}
# Igual que el validador de RegistroAtomico.dosis: lo que no pase va a `condiciones`
# ("500 cc y 5 litros por hectárea" no lo acepta el validador; antes se perdía el uso entero).
PATRON_AREA = re.compile(r'/(?:\s*)?(ha|mz)\b|por\s+(ha|mz)\b', re.IGNORECASE)

SYSTEM_ESTRUCTURA = """
Recibes el texto de UNA ficha técnica (o etiqueta, panfleto u hoja de seguridad) de un producto agrícola o pecuario que vende AldeaZON en Nicaragua.
Devuelve SOLO un objeto JSON con esta forma:
{
  "producto": "nombre comercial exacto",
  "principio_activo": "ingrediente(s) activo(s) o composición principal, o null",
  "tipo": "fungicida | insecticida | herbicida | nematicida | acaricida | fertilizante | foliar | bioestimulante | coadyuvante | semilla | enmienda | veterinario | otro",
  "descripcion": "para qué sirve y cómo actúa, en 1-3 frases tomadas de la ficha",
  "usos": [
    {"cultivo": "cultivo tal como lo nombra la ficha", "objetivo": "plaga, enfermedad, maleza o etapa/fin del cultivo, o null",
     "dosis": "dosis COPIADA LITERALMENTE de la ficha con su unidad, o null", "condiciones": "momento, frecuencia, volumen de agua, intervalo, carencia, etc., o null"}
  ],
  "precauciones": "restricciones, toxicidad, período de reingreso o carencia generales, en 1-2 frases, o null"
}
Reglas:
- Un elemento de "usos" por cada combinación cultivo + objetivo que traiga la ficha (también las de tablas cuyo texto viene desordenado).
- "dosis": copie la cifra y la unidad exactamente como aparecen (por ejemplo "0,7-1,5 L/ha", "2 ml por Lt. de agua", "500 cc/200 L"). Nunca calcule, convierta ni complete cifras. Si no hay dosis para ese uso, null.
- No invente cultivos, plagas ni datos que no estén en el texto. Si la ficha no trae usos por cultivo, "usos" = [].
- Responda en español.
""".strip()


@dataclass
class ResultadoFicha:
    documento: str
    metodo: str                      # 'prosa' | 'vision'
    registros: List[RegistroAtomico] = field(default_factory=list)
    caracteres_texto: int = 0
    usos_llm: int = 0
    dosis_descartadas: List[str] = field(default_factory=list)
    error: Optional[str] = None


def _sin_espacios(texto: str) -> str:
    return re.sub(r"\s+", "", texto or "").lower()


def dosis_anclada(dosis: Optional[str], texto_fuente_normalizado: str) -> bool:
    """
    True si cada cifra de la dosis está en la ficha y su unidad (con el área, si la trae)
    también aparece en la ficha (ignorando espacios y mayúsculas). Sin cifras con unidad,
    se exige la dosis completa literal.
    """
    if not dosis:
        return False
    cifras = CIFRA_CON_UNIDAD.findall(dosis)
    if not cifras:
        return _sin_espacios(dosis) in texto_fuente_normalizado
    return all(
        _sin_espacios(numero) in texto_fuente_normalizado
        and _sin_espacios(unidad + area) in texto_fuente_normalizado
        for numero, unidad, area in cifras
    )


def _recortar(texto: Optional[str], limite: int) -> Optional[str]:
    if texto is None:
        return None
    texto = re.sub(r"\s+", " ", str(texto)).strip().rstrip(".").strip()
    if not texto or texto.lower() in VALORES_NULOS:
        return None
    return texto if len(texto) <= limite else texto[: limite - 1].rstrip() + "…"


def leer_texto_pdf(filepath: str) -> str:
    with pdfplumber.open(filepath) as pdf:
        return "\n".join((page.extract_text() or "") for page in pdf.pages)


def estructurar_ficha(client: OpenAI, texto: str, filename: str) -> dict:
    response = client.chat.completions.create(
        model=MODELO,
        messages=[
            {"role": "system", "content": SYSTEM_ESTRUCTURA},
            {"role": "user", "content": f"ARCHIVO: {filename}\n\n{texto[:MAX_CARACTERES_LLM]}"},
        ],
        temperature=0.0,
        max_tokens=4000,
        response_format={"type": "json_object"},
    )
    return json.loads(response.choices[0].message.content)


def construir_registros(datos: dict, texto: str, filename: str, metodo: str, version: int,
                        resultado: ResultadoFicha) -> List[RegistroAtomico]:
    """Convierte el JSON del LLM en RegistroAtomico, descartando dosis que no están en la ficha."""
    fuente = _sin_espacios(texto)
    producto = _recortar(datos.get("producto"), 120) or re.sub(r"\.pdf$", "", filename, flags=re.I)
    principio = _recortar(datos.get("principio_activo"), 200)
    tipo = _recortar(datos.get("tipo"), 60)
    comunes = dict(documento_fuente=filename, version_corpus=version, producto=producto,
                   principio_activo=principio, metodo_extraccion=metodo,
                   estado_validacion="en_revision", idioma="es")

    registros, cultivos = [], []
    usos = datos.get("usos") or []
    resultado.usos_llm = len(usos)
    for uso in usos:
        if not isinstance(uso, dict):
            continue
        cultivo = _recortar(uso.get("cultivo"), 150) or "(varios/todos)"
        dosis = _recortar(uso.get("dosis"), 120)
        if dosis and not dosis_anclada(dosis, fuente):
            resultado.dosis_descartadas.append(dosis)
            dosis = None
        condiciones = _recortar(uso.get("condiciones"), MAX_CONDICIONES)
        dosis_area = dosis if dosis and PATRON_AREA.search(dosis) else None
        if dosis and not dosis_area:
            # p. ej. "2 ml por Lt. de agua": real, pero RegistroAtomico.dosis exige /ha o /mz
            condiciones = _recortar(f"Dosis: {dosis}. {condiciones or ''}", MAX_CONDICIONES)
        try:
            registros.append(RegistroAtomico(
                registro_id=str(uuid.uuid4()), cultivo=cultivo,
                objetivo=_recortar(uso.get("objetivo"), 200),
                dosis=dosis_area, condiciones=condiciones, **comunes,
            ))
            cultivos.append(cultivo)
        except ValidationError as exc:
            logger.warning(f"[{filename}] uso descartado ({cultivo}): {exc.errors()[0]['msg']}")

    general = " ".join(p for p in (
        _recortar(datos.get("descripcion"), 500),
        f"Precauciones: {_recortar(datos.get('precauciones'), 350)}" if datos.get("precauciones") else None,
    ) if p)
    registros.append(RegistroAtomico(
        registro_id=str(uuid.uuid4()),
        cultivo=_recortar(", ".join(dict.fromkeys(cultivos)), 300) or "(varios/todos)",
        objetivo=f"Ficha general ({tipo})" if tipo else "Ficha general",
        dosis=None, condiciones=_recortar(general, MAX_GENERAL), **comunes,
    ))
    return registros


def extraer_ficha(filepath: str, filename: str, version: int, client: Optional[OpenAI] = None) -> ResultadoFicha:
    """Extrae una ficha completa. Nunca lanza: el error queda en ResultadoFicha.error."""
    client = client or OpenAI(api_key=settings.openai_api_key)
    resultado = ResultadoFicha(documento=filename, metodo="prosa")
    try:
        texto = leer_texto_pdf(filepath)
        if len(texto.strip()) < MIN_CARACTERES_TEXTO:
            logger.info(f"[{filename}] {len(texto.strip())} caracteres de texto: transcripción por visión")
            resultado.metodo = "vision"
            texto = transcribir_con_vision(filepath, filename, client)
        resultado.caracteres_texto = len(texto)
        if len(texto.strip()) < 50:
            resultado.error = "Sin texto legible (ni por texto ni por visión)"
            return resultado
        datos = estructurar_ficha(client, texto, filename)
        resultado.registros = construir_registros(datos, texto, filename, resultado.metodo, version, resultado)
    except Exception as exc:
        resultado.error = f"{type(exc).__name__}: {str(exc)[:300]}"
        logger.error(f"[{filename}] extracción fallida: {resultado.error}")
    return resultado
