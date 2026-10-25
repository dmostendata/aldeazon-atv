# tests/test_traza.py
import asyncio

from app.services.traza import iniciar_traza, medido, paso


def test_registra_pasos_secuenciales_y_paralelos():
    async def request():
        traza = iniciar_traza()
        with paso("Configuración", "postgres"):
            await asyncio.sleep(0.01)
        await asyncio.gather(medido("A", "llm", asyncio.sleep(0.02)), medido("B", "llm", asyncio.sleep(0.02)))
        return traza.resumen()

    r = asyncio.run(request())
    assert [p["paso"] for p in r["pasos"]][0] == "Configuración"
    a, b = [p for p in r["pasos"] if p["componente"] == "llm"]
    assert abs(a["inicio_ms"] - b["inicio_ms"]) < 5          # en paralelo: empiezan juntos
    assert r["por_componente"]["llm"] >= 40 and r["total_ms"] < 60


def test_fuera_de_un_request_no_falla():
    with paso("suelto", "cpu"):
        pass
