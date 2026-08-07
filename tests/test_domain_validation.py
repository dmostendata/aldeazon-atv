# tests/test_domain_validation.py
import pytest
from pydantic import ValidationError
from app.schemas.domain import RegistroAtomico

def test_dosage_safety_gate_rejects_missing_area_unit():
    """Ensure doses like '10 g/l' (pH/concentration) fail validation."""
    with pytest.raises(ValidationError) as exc_info:
        RegistroAtomico(
            registro_id="550e8400-e29b-41d4-a716-446655440000",
            documento_fuente="Ficha_Consento.pdf",
            version_corpus=1,
            producto="Consento 45 SC",
            principio_activo="Propamocarb",
            cultivo="Tomate",
            objetivo="Broca",
            dosis="10 g/l",  # INVALID: Missing /ha or /mz
            condiciones="Aplicar foliar",
            metodo_extraccion="tabla",
            estado_validacion="aprobado",
            idioma="es"
        )
    assert "Invalid dosage format missing area unit" in str(exc_info.value)

def test_dosage_safety_gate_accepts_valid_hectare_unit():
    """Ensure valid doses with /ha pass validation."""
    record = RegistroAtomico(
        registro_id="550e8400-e29b-41d4-a716-446655440000",
        documento_fuente="Ficha_Consento.pdf",
        version_corpus=1,
        producto="Consento 45 SC",
        principio_activo="Propamocarb",
        cultivo="Tomate",
        objetivo="Broca",
        dosis="1,42 - 2,14 l/ha",  # VALID
        condiciones="Aplicar foliar",
        metodo_extraccion="tabla",
        estado_validacion="aprobado",
        idioma="es"
    )
    assert record.dosis == "1,42 - 2,14 l/ha"