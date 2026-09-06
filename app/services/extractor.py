# app/services/extractor.py
import re
import io
import base64
import logging
import uuid
from typing import Iterator
import pdfplumber
import pypdfium2 as pdfium
from openai import OpenAI

from app.core.config import settings
from app.schemas.domain import RegistroAtomico

logger = logging.getLogger("atv.extractor")

REGEX_DOSIS = re.compile(
    r'\d[\d.,]*\s*(?:-\s*\d[\d.,]*\s*)?\s*(?:kg|g|l|lt|ml|cc|%|qq|lbs|quintales)\s*/(?:\s*)?(?:ha|mz)\b',
    re.IGNORECASE
)

REGEX_CULTIVOS = re.compile(
    r'(tomate|papa|ma[íi]z|caf[eé]|cacao|man[íi]|soya|frijol|arroz|c[íi]trico|mel[oó]n|sand[íi]a|chile|banano)',
    re.IGNORECASE
)

SYSTEM_VISION = """
Eres un transcriptor de documentos técnicos agrícolas. Recibes la imagen de una página de ficha técnica. Tu única tarea es transcribirla fielmente a Markdown. Reglas estrictas:
1. Transcribe TODO el texto visible, incluido el que está dentro de fotografías de empaques o etiquetas.
2. Las tablas se transcriben como tablas Markdown, preservando encabezados y la correspondencia exacta fila-columna.
3. NO resumas, NO interpretes, NO corrijas, NO completes datos que no estén visibles. Si un valor es ilegible escribe [ILEGIBLE].
4. No agregues texto que no esté en la imagen. Nada de introducciones ni conclusiones propias.
""".strip()

def _resolver_nombre_producto(client: OpenAI, texto_fuente: str, filename: str) -> str:
    """
    Determines the commercial product name for a technical sheet from its
    extracted text/markdown.

    WHY THIS EXISTS: both extraction paths used to hardcode a placeholder
    ("[Transcrito por Visión]" / "[Extraído Automáticamente]") instead of
    the real brand name. Since texto_para_embedding() puts the product name
    first in every embedded chunk, that placeholder was identical across
    every document in the corpus and dominated/polluted similarity search —
    retrieval ended up matching on crop/dosage text instead of the actual
    product, causing wrong documents to be retrieved for product-specific
    queries.

    Called ONCE per file (not per page/line), so the added cost is one
    small text completion per document, not per chunk.
    """
    fragmento = texto_fuente[:4000]  # product name always appears early on
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": (
                    "Extrae ÚNICAMENTE el nombre comercial del producto agrícola "
                    "descrito en esta ficha técnica (ej. 'Engorlid', 'Consento 45 SC'). "
                    "Responde solo con el nombre exacto, sin explicaciones ni comillas."
                )},
                {"role": "user", "content": fragmento}
            ],
            temperature=0.0,
            max_tokens=20
        )
        nombre = (response.choices[0].message.content or "").strip().strip('"').strip("'")
        if nombre and len(nombre) >= 2:
            return nombre
    except Exception as exc:
        logger.warning(f"[{filename}] Product-name resolution failed, using filename fallback: {exc}")

    # Last-resort fallback: derive something readable from the filename
    # instead of a dead placeholder that would pollute every embedding
    # from this document identically.
    guess = re.sub(r'\.pdf$', '', filename, flags=re.IGNORECASE)
    guess = re.sub(r'(?i)\b(ficha[\s_-]*t[eé]cnica|hoja[\s_-]*de[\s_-]*seguridad|ft|tds|msds)\b', '', guess)
    guess = re.sub(r'[_\-\[\]\(\)0-9]+', ' ', guess).strip()
    return guess or "Producto sin identificar"


def extraer_desde_pdf(filepath: str, filename: str) -> Iterator[RegistroAtomico]:
    text_length = 0
    has_images = False

    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                text_length += len(text)
            if page.images:
                has_images = True

    if text_length < 1300 and has_images:
        logger.info(f"[{filename}] Routed to VISION path (Length: {text_length}).")
        yield from _via_vision(filepath, filename)
        return

    logger.info(f"[{filename}] Routed to DETERMINISTIC path.")
    registros = list(_cascada_determinista(filepath, filename))
    
    con_dosis = sum(1 for r in registros if r.dosis is not None)
    
    if con_dosis >= 1:
        yield from registros
    else:
        logger.warning(f"[{filename}] Deterministic path yielded 0 dosages. Fallback to VISION.")
        yield from _via_vision(filepath, filename)

def _cascada_determinista(filepath: str, filename: str) -> Iterator[RegistroAtomico]:
    coincidencias = []  # (cultivo, dosis) pairs found across all pages
    texto_paginas = []

    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            texto_paginas.append(text)
            for line in text.splitlines():
                if REGEX_CULTIVOS.search(line) and (m := REGEX_DOSIS.search(line)):
                    coincidencias.append((line[:m.start()].strip(' .:-|'), m.group(0)))

    if not coincidencias:
        return

    client = OpenAI(api_key=settings.openai_api_key)
    producto = _resolver_nombre_producto(client, "\n".join(texto_paginas), filename)

    for cultivo, dosis in coincidencias:
        yield RegistroAtomico(
            registro_id=str(uuid.uuid4()),
            documento_fuente=filename,
            version_corpus=1,
            producto=producto,
            cultivo=cultivo,
            dosis=dosis,
            metodo_extraccion="columnas",
            # Previously "aprobado" (auto-approved, no human review) on the
            # assumption the placeholder producto would never be trusted
            # downstream. Now that producto is an LLM-inferred real name,
            # it deserves the same review gate as the vision path.
            estado_validacion="en_revision",
            idioma="es"
        )

def _via_vision(filepath: str, filename: str) -> Iterator[RegistroAtomico]:
    client = OpenAI(api_key=settings.openai_api_key)
    markdown_pages = []

    with pdfium.PdfDocument(filepath) as pdf:
        for i in range(len(pdf)):
            page = pdf[i]
            bitmap = page.render(scale=200/72)
            pil_image = bitmap.to_pil()
            
            img_byte_arr = io.BytesIO()
            pil_image.save(img_byte_arr, format='PNG')
            base64_image = base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')

            try:
                response = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": SYSTEM_VISION},
                        {"role": "user", "content": [
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_image}"}}
                        ]}
                    ],
                    temperature=0.0
                )
                markdown_pages.append(response.choices[0].message.content)
            except Exception as exc:
                logger.error(f"[{filename}] Vision API Error on page {i}: {exc}")

    combined_markdown = "\n".join(markdown_pages)

    coincidencias = []
    for line in combined_markdown.splitlines():
        clean_line = line.replace('|', ' ').strip()
        if REGEX_CULTIVOS.search(clean_line) and (m := REGEX_DOSIS.search(clean_line)):
            coincidencias.append((
                clean_line[:m.start()].strip(' .|-*'),
                m.group(0),
                clean_line[m.end():].strip(' .|-*') or None,
            ))

    if not coincidencias:
        return

    producto = _resolver_nombre_producto(client, combined_markdown, filename)

    for cultivo, dosis, condiciones in coincidencias:
        yield RegistroAtomico(
            registro_id=str(uuid.uuid4()),
            documento_fuente=filename,
            version_corpus=1,
            producto=producto,
            cultivo=cultivo,
            dosis=dosis,
            condiciones=condiciones,
            metodo_extraccion="vision",
            estado_validacion="en_revision",
            idioma="es"
        )