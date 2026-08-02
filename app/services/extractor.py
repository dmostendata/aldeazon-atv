# app/services/extractor.py
import re
import uuid
import logging
from typing import Iterator
import pdfplumber
import pypdfium2 as pdfium
from pdfminer.pdfparser import PDFSyntaxError

from app.schemas.registro_atomico import RegistroAtomico

logger = logging.getLogger(__name__)

# Expresiones regulares precompiladas a nivel de módulo (boot time)
# DOSIS_REGEX exige unidad de área (ha|mz) para evitar falsos positivos de pH (Blueprint §6.5.3)[cite: 12]
DOSIS_REGEX = re.compile(
    r'\d[\d.,]*\s*(?:-\s*\d[\d.,]*\s*)?\s*(?:kg|g|l|lt|ml|cc)\s*/\s*(?:ha|mz)', 
    re.IGNORECASE
)

CULTIVOS_REGEX = re.compile(
    r'(tomate|papa|ma[íi]z|caf[éee]|cacao|man[íi]|soya|frijol|arroz|c[íi]trico|mel[oó]n|sand[íi]a|chile|banano)', 
    re.IGNORECASE
)


def extraer_desde_pdf(filepath: str, nombre_archivo: str) -> Iterator[RegistroAtomico]:
    """
    Router de Extracción (ODAI Blueprint §6.5.2)[cite: 12].

    intentamos primero el driver principal ('pdfplumber') y, si el parser 
    estricto falla por sintaxis PDF/XREF, hacemos fallback automático a un motor 
    C++ tolerante a fallos ('pypdfium2').
    """
    registros_extraidos = 0

    # 1. INTENTO DETERMINISTA PRINCIPAL (pdfplumber / pdfminer.six)[cite: 12]
    try:
        with pdfplumber.open(filepath) as pdf:
            for page in pdf.pages:
                texto = page.extract_text()
                if not texto:
                    continue
                for linea in texto.splitlines():
                    if (cultivo := CULTIVOS_REGEX.search(linea)) and (dosis := DOSIS_REGEX.search(linea)):
                        registros_extraidos += 1
                        yield _crear_registro(nombre_archivo, cultivo.group(0), dosis.group(0), linea)
                        
        if registros_extraidos > 0:
            return

    except (PDFSyntaxError, Exception) as exc:
        # Interceptamos fallos de tabla XREF o catálogo /Root roto sin abortar
        logger.warning(
            f"[{nombre_archivo}] Parser estricto falló ({type(exc).__name__}). "
            "Activando sanación de XREF en memoria con motor C++ (pypdfium2)..."
        )

    # 2. FALLBACK DE SANACIÓN EN MEMORIA (pypdfium2 / Google PDFium C++)
    # Al igual que Chrome o Preview, PDFium repara tablas XREF corruptas sobre la marcha.
    try:
        doc = pdfium.PdfDocument(filepath)
        for i in range(len(doc)):
            page = doc[i]
            text_page = page.get_textpage()
            texto = text_page.get_text_range()
            
            if not texto:
                continue
                
            for linea in texto.splitlines():
                if (cultivo := CULTIVOS_REGEX.search(linea)) and (dosis := DOSIS_REGEX.search(linea)):
                    registros_extraidos += 1
                    yield _crear_registro(nombre_archivo, cultivo.group(0), dosis.group(0), linea)
                    
        doc.close()
        
        if registros_extraidos > 0:
            logger.info(f"[{nombre_archivo}] Sanación C++ exitosa. Extraídos {registros_extraidos} registros.")
            return

    except Exception as exc_pdfium:
        logger.error(f"[{nombre_archivo}] Fallo en motor C++ PDFium: {str(exc_pdfium)}")

    # 3. RUTA DE VISIÓN (ODAI Blueprint §6.5.4 - LLM Multimodal)[cite: 12]
    # Si las reglas deterministas no logran extraer dosis (e.g., tablas en imágenes estáticas),
    # se escala a la transcripción multimodal[cite: 12].
    if registros_extraidos == 0:
        logger.warning(f"[{nombre_archivo}] 0 dosis encontradas por texto. Escalando a Ruta de Visión (§6.5.4)...")
        yield from _extraer_por_vision(filepath, nombre_archivo)


def _crear_registro(nombre_archivo: str, cultivo: str, dosis: str, condiciones: str) -> RegistroAtomico:
    """Fábrica de DTO para homogeneizar la creación del Registro Atómico[cite: 12]."""
    return RegistroAtomico(
        registro_id=str(uuid.uuid4()),
        documento_fuente=nombre_archivo,
        version_corpus=1,
        producto="Producto Detectado",
        cultivo=cultivo,
        dosis=dosis,
        condiciones=condiciones.strip(),
        metodo_extraccion="columnas",
        estado_validacion="aprobado",
        idioma="es"
    )


def _extraer_por_vision(filepath: str, nombre_archivo: str) -> Iterator[RegistroAtomico]:
    """
    Ruta de Visión (ODAI Blueprint §6.5.4)[cite: 12].
    Placeholder arquitectónico listo para conectar a OpenAI Vision / Gemini Flash 
    cuando el documento consiste de imágenes escaneadas sin capa de texto.
    """
    # En desarrollo local: evitamos lanzar excepción no controlada si la API no está inyectada.
    # Cuando se ejecute en VPS2 (Airflow), aquí se invoca el cliente multimodal con SYSTEM_VISION[cite: 12].
    return iter(())