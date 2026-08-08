# app/services/pii_sanitizer.py
import re
import time
import logging
from typing import Tuple

logger = logging.getLogger("atv.sanitizer")

# -------------------------------------------------------------------------
# LEY 787 COMPLIANCE: NLP & REGEX INITIALIZATION
# -------------------------------------------------------------------------
try:
    # Load lightweight Spanish NLP model for Named Entity Recognition (NER)
    # Memory footprint: ~15MB. Execution time: ~15-30ms per prompt.
    import spacy
    nlp = spacy.load("es_core_news_sm")
except ImportError:
    logger.error("spaCy not installed. Run: pip install spacy")
    nlp = None
except OSError:
    logger.warning("Model 'es_core_news_sm' not found. Run: python -m spacy download es_core_news_sm")
    nlp = None

# Nicaraguan National ID: 14 digits followed by an uppercase letter (e.g., 001-010190-0000A)
REGEX_CEDULA = re.compile(r'\b\d{3}-?\d{6}-?\d{4}[A-Za-z]\b')

# Nicaraguan Phone Numbers: 8 digits starting with 5, 7, or 8 (Claro/Tigo)
REGEX_TELEFONO = re.compile(r'\b[578]\d{7}\b')

# -------------------------------------------------------------------------
# RSK-001 GOVERNANCE: DETERMINISTIC INTENT ROUTING
# -------------------------------------------------------------------------
# ODAI Blueprint §6.6.3: Identifies high-risk agronomic queries
REGEX_RIESGO = re.compile(
    r'(dosis|cu[aa]nt[oa]s?\s+(aplico|echo|uso)|qu[eé]\s+producto|'
    r'mezclar|aplicar|l(itros)?\s*/\s*(ha|mz)|tratamiento\s+para)',
    re.IGNORECASE
)


def sanitizar_pii(texto: str) -> Tuple[str, int]:
    """
    Ley 787 Compliance Gate: Scrubs PII from prompts before external transmission.
    Returns: Tuple[Sanitized Text, Latency in ms]
    """
    t0 = time.perf_counter()
    
    # 1. Fast deterministic scrub (Regex)
    texto_seguro = REGEX_CEDULA.sub('[CEDULA]', texto)
    texto_seguro = REGEX_TELEFONO.sub('[TELEFONO]', texto_seguro)
    
    # 2. Heuristic scrub (NER) for proper names
    if nlp:
        doc = nlp(texto_seguro)
        for ent in doc.ents:
            if ent.label_ == "PER":
                # Only replace the exact matched string to preserve agronomic context
                texto_seguro = texto_seguro.replace(ent.text, '[NOMBRE]')
                
    lat_ms = int((time.perf_counter() - t0) * 1000)
    return texto_seguro, lat_ms


def clasificar_intencion(texto: str) -> Tuple[str, int]:
    """
    Intent Classification Gate: Determines required confidence thresholds.
    Execution target: <= 0.3s.
    Returns: Tuple[Intent String, Latency in ms]
    """
    t0 = time.perf_counter()
    
    # Defaults to informational unless action-oriented keywords are detected
    intencion = "recomendacion" if REGEX_RIESGO.search(texto) else "informativa"
        
    lat_ms = int((time.perf_counter() - t0) * 1000)
    return intencion, lat_ms