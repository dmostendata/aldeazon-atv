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
    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for line in text.splitlines():
                if REGEX_CULTIVOS.search(line) and (m := REGEX_DOSIS.search(line)):
                    yield RegistroAtomico(
                        registro_id=str(uuid.uuid4()),
                        documento_fuente=filename,
                        version_corpus=1,
                        producto="[Extraído Automáticamente]", 
                        cultivo=line[:m.start()].strip(' .:-|'),
                        dosis=m.group(0),
                        metodo_extraccion="columnas",
                        estado_validacion="aprobado", 
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
    for line in combined_markdown.splitlines():
        clean_line = line.replace('|', ' ').strip()
        if REGEX_CULTIVOS.search(clean_line) and (m := REGEX_DOSIS.search(clean_line)):
            yield RegistroAtomico(
                registro_id=str(uuid.uuid4()),
                documento_fuente=filename,
                version_corpus=1,
                producto="[Transcrito por Visión]",
                cultivo=clean_line[:m.start()].strip(' .|-*'),
                dosis=m.group(0),
                condiciones=clean_line[m.end():].strip(' .|-*') or None,
                metodo_extraccion="vision",
                estado_validacion="en_revision",
                idioma="es"
            )