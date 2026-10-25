# tests/test_faq.py
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import numpy as np

from app.services.faq_service import Candidata, IndiceFAQ, PreguntaFrecuente, elegir_faq
from app.services.prompt_config import construir_prompt_config
from app.services.rag_service import ATVRagService
from scripts.cargar_preguntas_frecuentes import leer_excel, separar_variantes
from tests.test_prompt_config import FILAS_VALIDAS

CFG = construir_prompt_config(FILAS_VALIDAS)
EXCEL = "docs/Preguntas_y_Respuestas_AldeaZON rellenado.xlsx"

ROYA = PreguntaFrecuente("FAQ-001", "Café", "¿Qué tiene para roya?", ("¿Qué tiene para rolla?",),
                         "Primero confirme que es roya en el envés.", "Dar una dosis sin revisar la hoja.")
BROCA = PreguntaFrecuente("FAQ-002", "Café", "¿Qué tiene para broca?", (), "Evalúe frutos perforados.", None)


class FakeEmbedding:
    """Vector = conteo de palabras clave; suficiente para ordenar candidatas."""
    CLAVES = ("roya", "rolla", "broca", "maíz")

    def embed(self, textos):
        for t in textos:
            yield np.array([t.lower().count(c) + 0.01 for c in self.CLAVES])


def _indice(*faqs):
    indice = IndiceFAQ()
    indice._faqs = list(faqs)
    textos, dueno = [], []
    for i, f in enumerate(faqs):
        for t in (f.pregunta, *f.variantes):
            textos.append(t)
            dueno.append(i)
    m = np.array(list(FakeEmbedding().embed(textos)))
    indice._matriz = m / np.linalg.norm(m, axis=1, keepdims=True)
    indice._dueno = np.array(dueno)
    return indice


def _llm(contenido=None, error=None):
    client = MagicMock()
    if error:
        client.chat.completions.create = AsyncMock(side_effect=error)
    else:
        client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=contenido))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        ))
    return client


def test_candidatas_usan_tambien_las_variantes():
    candidatas = _indice(BROCA, ROYA).candidatas(FakeEmbedding(), "tengo rolla en el cafe", 2)
    assert candidatas[0].faq.codigo == "FAQ-001"
    assert len(candidatas) == 2


def test_indice_vacio_no_da_candidatas():
    assert IndiceFAQ().candidatas(FakeEmbedding(), "roya", 5) == []


def test_juez_elige_codigo_de_las_candidatas():
    cands = [Candidata(ROYA, 0.9), Candidata(BROCA, 0.5)]
    elegida = asyncio.run(elegir_faq(_llm('{"codigo": "FAQ-001"}'), CFG, "rolla", "Café", cands))
    assert elegida.faq is ROYA


def test_juez_no_puede_inventar_codigo():
    cands = [Candidata(ROYA, 0.9)]
    assert asyncio.run(elegir_faq(_llm('{"codigo": "FAQ-999"}'), CFG, "x", None, cands)) is None
    assert asyncio.run(elegir_faq(_llm('{"codigo": null}'), CFG, "x", None, cands)) is None


def test_juez_falla_cerrado():
    cands = [Candidata(ROYA, 0.9)]
    assert asyncio.run(elegir_faq(_llm(error=TimeoutError("lento")), CFG, "x", None, cands)) is None
    assert asyncio.run(elegir_faq(_llm("no es json"), CFG, "x", None, cands)) is None


FICHA_OPERA = {"documento_fuente": "FT Opera.pdf", "producto": "OPERA", "cultivo": "Café – roya",
               "dosis": "0.75 l/mz", "condiciones": None}


def _servicio(llm, fichas=(FICHA_OPERA,), error_qdrant=None):
    servicio = ATVRagService.__new__(ATVRagService)
    servicio.llm_client = llm
    servicio.db = None  # sin producto elegido no se consulta el inventario
    servicio._buscar_fichas = AsyncMock(
        side_effect=error_qdrant, return_value=[SimpleNamespace(payload=f) for f in fichas]
    )
    return servicio


def _responder(llm, **kwargs):
    return asyncio.run(_servicio(llm, **kwargs)._responder_faq(
        Candidata(ROYA, 0.93), "¿qué tiene para rolla?", "recomendacion", "Cultivo: Café", "café", CFG,
        lat_sanitizacion=1, lat_clasif=2, lat_faq=3,
    ))


def test_respuesta_faq_usa_el_llm_y_cita_la_faq():
    r = _responder(_llm("Primero revise el envés de la hoja. [Fuente: Preguntas frecuentes de Asistencia Técnica, FAQ-001]"))
    assert r.fuente == "Preguntas frecuentes de Asistencia Técnica, FAQ-001"
    assert r.modelo_utilizado.endswith("_faq")
    assert r.registros_usados[0]["codigo"] == "FAQ-001"
    assert not r.escalada_hitl and r.carrito == []


def test_respuesta_faq_con_dosis_inventada_entrega_la_aprobada():
    r = _responder(_llm("Aplique 2 l/mz de fungicida."))
    assert r.modelo_utilizado == "faq_literal"
    assert r.respuesta.startswith(ROYA.respuesta)


def test_respuesta_faq_sin_llm_entrega_la_aprobada():
    r = _responder(_llm(error=TimeoutError("lento")))
    assert r.modelo_utilizado == "faq_literal"
    assert "FAQ-001" in r.respuesta


def test_separar_variantes():
    assert separar_variantes("¿Sirve Yara Mila? ¿Cómo lo aplico?") == ["¿Sirve Yara Mila?", "¿Cómo lo aplico?"]
    assert separar_variantes("hojo de gallo") == ["hojo de gallo"]
    assert separar_variantes(None) == []


def test_leer_excel_real():
    registros = leer_excel(EXCEL)
    codigos = [r["codigo"] for r in registros]
    assert "EJEMPLO" not in codigos and len(codigos) == len(set(codigos)) >= 100
    roya = next(r for r in registros if r["codigo"] == "FAQ-001")
    assert roya["cultivo"] == "Café" and roya["respuesta_evitar"] and roya["variantes"]


def test_respuesta_faq_recibe_las_fichas_y_acepta_su_dosis():
    llm = _llm("Confirme la roya. Producto AldeaZON recomendado: OPERA, 0.75 l/mz. [Fuente: FAQ-001]")
    r = _responder(llm)
    prompt = llm.chat.completions.create.call_args.kwargs["messages"][1]["content"]
    assert "OPERA" in prompt and "0.75 l/mz" in prompt
    assert r.modelo_utilizado.endswith("_faq")  # la dosis está en la ficha: no se rechaza
    assert any(reg.get("producto") == "OPERA" for reg in r.registros_usados)


def test_respuesta_faq_sin_qdrant_responde_igual():
    llm = _llm("Confirme la roya. Producto AldeaZON recomendado: en su tienda AldeaZON le ayudamos.")
    r = _responder(llm, error_qdrant=RuntimeError("qdrant caído"))
    prompt = llm.chat.completions.create.call_args.kwargs["messages"][1]["content"]
    assert "Sin fichas de productos" in prompt
    assert r.modelo_utilizado.endswith("_faq")


def test_texto_de_busqueda_suma_el_cultivo_solo_si_hace_falta():
    from app.services.rag_service import nombre_corto_producto, texto_de_busqueda
    productos = {nombre_corto_producto(p) for p in ("ROOT-PLUS", "MANCUERNA PLUS 28 SE", "EVITO® T 45 SC")}
    assert "rootplus" in productos and "mancuernaplus" in productos
    assert texto_de_busqueda("¿Qué tiene para la roya?", "café", productos) == "¿Qué tiene para la roya? en café"
    assert texto_de_busqueda("mosca blanca en tomate", "café", productos) == "mosca blanca en tomate"
    assert texto_de_busqueda("¿Cuánto Root Plus le echo?", "café", productos) == "¿Cuánto Root Plus le echo?"
    assert texto_de_busqueda("¿Qué tiene para broca?", None, productos) == "¿Qué tiene para broca?"


def test_groundedness_misma_dosis_con_otra_ortografia():
    from app.services.rag_service import validar_groundedness_dosis
    ficha = "Producto: EVITO T | Dosis: 0.3 Lt/Ha | Condiciones: intervalo 15 días"
    assert validar_groundedness_dosis("Aplique 0.3 L/ha cada 15 días.", ficha)
    assert validar_groundedness_dosis("Aplique 0,3 litros/ha.", ficha)
    assert not validar_groundedness_dosis("Aplique 0.5 L/ha.", ficha)       # otra cifra
    assert not validar_groundedness_dosis("Aplique 0.3 L/mz.", ficha)       # otra área
    assert not validar_groundedness_dosis("Aplique 0.3 kg/ha.", ficha)      # otra unidad
