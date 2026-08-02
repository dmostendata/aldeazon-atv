# app/services/extractor.py
import re
import uuid
import logging
import pdfplumber
from typing import Iterator
from app.schemas.registro_atomico import RegistroAtomico

# Python's equivalent to Laravel's Log facade.
# We configure it to capture warnings and errors at the module level.
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# Module-level regex compilation (evaluated once at boot time)
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
    Parses a PDF and yields strongly-typed RegistroAtomico instances.
    Implements Extractor 2 (Columnas alineadas) from the Blueprint.
    """
    try:
        # pdfplumber.open acts like a file handle. If the file is not a valid PDF,
        # it raises pdfminer.pdfparser.PDFSyntaxError.
        with pdfplumber.open(filepath) as pdf:
            for page in pdf.pages:
                texto = page.extract_text()
                if not texto:
                    continue
                    
                for linea in texto.splitlines():
                    cultivo_match = CULTIVOS_REGEX.search(linea)
                    dosis_match = DOSIS_REGEX.search(linea)
                    
                    if cultivo_match and dosis_match:
                        yield RegistroAtomico(
                            registro_id=str(uuid.uuid4()),
                            documento_fuente=nombre_archivo,
                            version_corpus=1, 
                            producto="Producto Detectado", 
                            cultivo=cultivo_match.group(0),
                            dosis=dosis_match.group(0),
                            condiciones=linea.strip(),
                            metodo_extraccion="columnas",
                            estado_validacion="aprobado",
                            idioma="es"
                        )
    
    # Catching Exception is equivalent to catch (\Exception $e) in PHP.
    except Exception as e:
        # Log the error. By doing nothing else (implicit return), the generator
        # gracefully stops yielding for this specific file. The consumer loop 
        # in the orchestrator script simply moves on to the next file.
        logger.error(f"Error procesando el archivo '{nombre_archivo}': {str(e)}")
        return