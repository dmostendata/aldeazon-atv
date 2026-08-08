# tests/test_sanitizer.py
from app.services.pii_sanitizer import sanitizar_pii, clasificar_intencion

def test_ley_787_sanitizer_scrubs_cedula_and_phone():
    """Ensure Nicaraguan IDs and cellular numbers are stripped."""
    raw_prompt = "El productor Carlos Fonseca con cédula 161-200278-0000A me llama al 88889999 para preguntar dosis."
    sanitized_text, lat_ms = sanitizar_pii(raw_prompt)
    
    assert "161-200278-0000A" not in sanitized_text
    assert "[CEDULA]" in sanitized_text
    assert "88889999" not in sanitized_text
    assert "[TELEFONO]" in sanitized_text
    
    # Assuming spaCy is installed and loaded correctly
    assert "Carlos Fonseca" not in sanitized_text
    assert "[NOMBRE]" in sanitized_text
    assert lat_ms < 300  # Enforce micro-latency SLA

def test_intent_classifier_detects_recommendation_risk():
    """Ensure agronomic dosing questions trigger the 'recomendacion' high-risk threshold."""
    prompts = [
        "¿Cuál es la dosis de Consento?",
        "¿Qué producto aplico para la broca?",
        "¿Cuántos litros/ha necesito?"
    ]
    
    for prompt in prompts:
        intencion, lat_ms = clasificar_intencion(prompt)
        assert intencion == "recomendacion"

def test_intent_classifier_defaults_to_informational():
    """Ensure standard catalog questions fall back to 'informativa'."""
    prompt = "¿Qué es el ingrediente activo del Consento?"
    intencion, _ = clasificar_intencion(prompt)
    
    assert intencion == "informativa"