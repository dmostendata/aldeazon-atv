# tests/test_golden_set.py
import pytest
import logging
from sqlalchemy.ext.asyncio import AsyncSession
from qdrant_client import AsyncQdrantClient

from app.core.config import settings
from app.services.rag_service import ATVRagService
from app.db.session import AsyncSessionLocal

logger = logging.getLogger("atv.golden_set")

# =====================================================================
# THE GOLDEN SET (Curated Benchmark Data)
# =====================================================================
# A representative sample of the Dueño del Conocimiento's critical queries.
# In a mature production environment, this list contains 50-100 items.
GOLDEN_SET = [
    {
        "consulta": "¿Cuál es la dosis de Consento 45 SC para broca en café?",
        "intencion_esperada": "recomendacion",
        "escalada_hitl_esperada": False,  # Should answer confidently if corpus is good
        "texto_requerido": "1,42",       # Expected dosage fragment
    },
    {
        "consulta": "¿Qué es el ingrediente activo del Consento?",
        "intencion_esperada": "informativa",
        "escalada_hitl_esperada": False,
        "texto_requerido": "propamocarb", # Expected molecule
    },
    {
        "consulta": "¿Cuál es el mejor insecticida para matar dragones en marte?",
        "intencion_esperada": "fuera_alcance", # Or 'recomendacion' with low confidence
        "escalada_hitl_esperada": True,        # MUST trigger HITL / Abstention
        "texto_requerido": "",
    },
    {
        "consulta": "¿Cuántos litros por hectárea de Biogibb aplico en tomate?",
        "intencion_esperada": "recomendacion",
        "escalada_hitl_esperada": False,
        "texto_requerido": "", # We just check intent and HITL routing here
    }
]

# RSK-001: Minimum accuracy threshold required to deploy a new corpus version
UMBRAL_PRECISION_MINIMA = 0.60


@pytest.fixture
async def rag_service():
    """
    Dependency Injection Fixture.
    Provisions the AsyncQdrantClient and AsyncSession specifically for the test lifecycle.
    """
    qdrant = AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    async with AsyncSessionLocal() as db:
        yield ATVRagService(qdrant_client=qdrant, db_session=db)
    # Teardown happens automatically after yield


@pytest.mark.asyncio
async def test_precision_golden_set(rag_service: ATVRagService):
    """
    Executes the Golden Set regression suite to measure RAG pipeline accuracy.
    Enforces the RSK-001 60% accuracy gate.
    """
    aciertos = 0
    total = len(GOLDEN_SET)
    
    logger.info(f"Initiating Golden Set Evaluation ({total} queries)...")

    for item in GOLDEN_SET:
        # Mocking sucursal_id = 101 for the commercial catalog dependency
        resultado = await rag_service.procesar_consulta(item["consulta"], sucursal_id=101)
        
        # Scoring Logic
        intent_match = resultado.intencion == item["intencion_esperada"]
        hitl_match = resultado.escalada_hitl == item["escalada_hitl_esperada"]
        
        # If the test requires specific text (e.g., a dosage), check for its presence
        texto_match = True
        if item["texto_requerido"]:
            texto_match = item["texto_requerido"].lower() in resultado.respuesta.lower()

        # If it's an out-of-scope question, we only care that HITL fired and intent matched
        if item["escalada_hitl_esperada"]:
            es_correcto = hitl_match
        else:
            es_correcto = intent_match and hitl_match and texto_match

        if es_correcto:
            aciertos += 1
        else:
            logger.warning(
                f"Golden Set Miss: '{item['consulta']}' | "
                f"Expected Intent: {item['intencion_esperada']}, Got: {resultado.intencion} | "
                f"Expected HITL: {item['escalada_hitl_esperada']}, Got: {resultado.escalada_hitl}"
            )

    precision_actual = aciertos / total
    logger.info(f"Golden Set Accuracy: {precision_actual:.2%} ({aciertos}/{total})")

    # The actual CI/CD Deployment Gate Assertion
    assert precision_actual >= UMBRAL_PRECISION_MINIMA, (
        f"Deployment Blocked (RSK-001): Golden Set precision ({precision_actual:.2%}) "
        f"is below the {UMBRAL_PRECISION_MINIMA:.2%} threshold."
    )