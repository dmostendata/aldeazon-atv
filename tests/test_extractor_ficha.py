# tests/test_extractor_ficha.py
from app.services.extractor_ficha import ResultadoFicha, _sin_espacios, construir_registros, dosis_anclada

TEXTO = """FLUAZIFOP 15 EC  CULTIVO PLAGA DOSIS
Frijol
Zacate Johnson 0,7-1,5 L/ha
Aplicar 2.5 ml por      (otra columna)
Lt. de agua en semillero"""


def test_dosis_anclada_por_cifra_y_unidad():
    fuente = _sin_espacios(TEXTO)
    assert dosis_anclada("0,7 - 1,5 L/ha", fuente)
    assert dosis_anclada("2.5 ml por Lt. de agua", fuente)   # partida en dos columnas
    assert not dosis_anclada("3 L/ha", fuente)                 # cifra inventada
    assert not dosis_anclada("0,7-1,5 mL/ha", fuente)          # unidad cambiada (la ficha dice L/ha)


def test_dosis_con_la_unidad_en_el_encabezado_de_la_tabla():
    fuente = _sin_espacios("CULTIVO PLAGA DOSIS L/ha\nManí Roya del maní 0.7-1.0")
    assert dosis_anclada("0.7-1.0 L/ha", fuente)
    assert not dosis_anclada("0.7-1.0 kg/ha", fuente)
    assert not dosis_anclada(None, fuente)


def _registros(usos, **extra):
    datos = {"producto": "Fluazifop 15 EC", "principio_activo": "null", "tipo": "herbicida",
             "descripcion": "Herbicida postemergente.", "usos": usos, "precauciones": None, **extra}
    resultado = ResultadoFicha(documento="f.pdf", metodo="prosa")
    return construir_registros(datos, TEXTO, "f.pdf", "prosa", 2, resultado), resultado


def test_dosis_por_area_va_a_dosis_y_por_litro_a_condiciones():
    regs, _ = _registros([
        {"cultivo": "Frijol", "objetivo": "Zacate Johnson", "dosis": "0,7-1,5 L/ha", "condiciones": None},
        {"cultivo": "Frijol", "objetivo": "null", "dosis": "2.5 ml por Lt. de agua", "condiciones": "semillero"},
    ])
    assert regs[0].dosis == "0,7-1,5 L/ha"
    assert regs[1].dosis is None and regs[1].condiciones.startswith("Dosis: 2.5 ml por Lt. de agua")
    assert regs[1].objetivo is None and regs[0].principio_activo is None   # "null" del LLM = vacío


def test_dosis_inventada_se_descarta_pero_el_uso_queda():
    regs, resultado = _registros([{"cultivo": "Frijol", "objetivo": "malezas", "dosis": "3 L/ha"}])
    assert regs[0].dosis is None and resultado.dosis_descartadas == ["3 L/ha"]


def test_siempre_hay_registro_general_aunque_no_haya_usos():
    regs, _ = _registros([])
    assert len(regs) == 1
    assert regs[0].objetivo == "Ficha general (herbicida)" and regs[0].cultivo == "(varios/todos)"
    assert "Herbicida postemergente" in regs[0].condiciones
