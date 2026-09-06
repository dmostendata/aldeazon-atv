# scripts/test_extractor.py
import os
import sys
import logging
from app.services.extractor import extraer_desde_pdf

logging.getLogger("httpx").setLevel(logging.WARNING)
STAGING_DIR = "/var/www/aldeazon-atv/fixtures/staging"

def test_single_file(target_filename: str) -> None:
    """
    MLOps Sandbox: Isolates the NLP extraction logic for rapid debugging.
    Usage: python scripts/test_extractor.py "filename.pdf"
    """
    filepath = os.path.join(STAGING_DIR, target_filename)
    
    if not os.path.exists(filepath):
        print(f"[!] FATAL: File not found in staging: {filepath}")
        return

    print(f"[*] =====================================================")
    print(f"[*] ISOLATED EXTRACTION TEST: {target_filename}")
    print(f"[*] =====================================================")

    try:
        registros = list(extraer_desde_pdf(filepath, target_filename))
        
        print(f"\n[*] Extraction Complete. Yielded {len(registros)} atomic tuples.")
        print(f"[*] -----------------------------------------------------")
        
        if not registros:
            print("[!] The Extractor Service yielded 0 records.")
            print("[!] Cause: The document lacks explicit dosages/crops, OR the regex missed them.")
            return

        for idx, reg in enumerate(registros, 1):
            print(f"\n[ Tuple #{idx} ]")
            print(f" - Producto:   {reg.producto}")
            print(f" - Cultivo:    {reg.cultivo}")
            print(f" - Dosis:      {reg.dosis}")
            print(f" - Método:     {reg.metodo_extraccion}")
            print(f" - Validación: {reg.estado_validacion}")
            
    except Exception as e:
        print(f"\n[!] EXTRACTION CRASHED: {type(e).__name__}")
        print(f"    {e}")

if __name__ == "__main__":
    # Fallback to Engorlid if no argument is passed
    target = sys.argv[1] if len(sys.argv) > 1 else "2022_NICARAGUA_FT_ENGORLID.pdf"
    test_single_file(target)