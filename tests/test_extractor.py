# tests/test_extractor.py
from app.services.extractor import REGEX_DOSIS

def test_dosage_regex_requires_area_unit():
    """Ensure doses like '10 g/l' (pH/concentration) are ignored, while '/ha' passes."""
    
    valid_doses = [
        "1,42 - 2,14 l/ha",
        "500 ml/mz",
        "2.5 kg/ha"
    ]
    invalid_doses = [
        "10 g/l",       # Missing area unit
        "pH 5.5",       # Irrelevant metric
        "100 cc"        # Missing area unit
    ]

    for dose in valid_doses:
        assert REGEX_DOSIS.search(dose) is not None, f"Failed to match valid dose: {dose}"
        
    for dose in invalid_doses:
        assert REGEX_DOSIS.search(dose) is None, f"Falsely matched invalid dose: {dose}"