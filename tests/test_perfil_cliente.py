# tests/test_perfil_cliente.py
import hashlib
import hmac
from datetime import date

import pytest

from app.services.perfil_cliente import (
    Cultivo, FincaResumen, LoteResumen, PerfilUsuario, area_a_mz, extraer_cedula,
    extraer_telefonos, hash_determinista, ocultar_telefonos,
)
from scripts.construir_perfiles import FotoInvalidaError, validar_fotos


def test_hash_igual_al_de_semillero():
    # core/masking.py de Semillero: HMAC-SHA256(salt, str(valor)).hexdigest()
    esperado = hmac.new(b"salt", b"001-010190-0000A", hashlib.sha256).hexdigest()
    assert hash_determinista("001-010190-0000A", "salt") == esperado


@pytest.mark.parametrize("texto", [
    "mi cédula es 001-010190-0000a", "cedula 0010101900000A por favor", "001-0101900000A",
])
def test_cedula_variantes(texto):
    variantes = extraer_cedula(texto)
    assert "001-010190-0000A" in variantes
    assert "001-010190-0000a" in variantes
    assert "0010101900000A" in variantes


def test_sin_cedula():
    assert extraer_cedula("tengo 3 manzanas de café") is None


@pytest.mark.parametrize("texto, esperado", [
    ("mi whatsapp 88887777", ["88887777"]),
    ("whatsapp: 8888-7777", ["88887777"]),
    ("+505 5888 7777", ["58887777"]),
    ("505-8888-7777 y 7777 8888", ["88887777", "77778888"]),
])
def test_telefonos(texto, esperado):
    assert extraer_telefonos(texto) == esperado


def test_telefono_no_se_confunde_con_cedula_ni_cifras():
    assert extraer_telefonos("cédula 001-010190-0000A, 1500 plantas a 1200 msnm") == []


def test_ocultar_telefonos():
    assert ocultar_telefonos("llámeme al 8888-7777") == "llámeme al [TELEFONO]"


def test_area_en_hectareas_pasa_a_manzanas():
    assert area_a_mz(7.026, "ha") == 10.0
    assert area_a_mz(3, "mz") == 3
    assert area_a_mz(0, "mz") is None


def _perfil_registrado() -> PerfilUsuario:
    return PerfilUsuario(
        tipo_usuario="socio", fuente="registro", verificado=True, persona_id=4321,
        es_asociado_activo=True, sucursal_id=7, es_organico=True,
        cultivos=[Cultivo(nombre="Café", area_mz=7.4), Cultivo(nombre="Frijol", area_mz=2)],
        municipio="Jinotega", departamento="Jinotega",
        fincas=[FincaResumen(area_total_mz=12, area_cafe_mz=7.5, altura_msnm=1150, topografia="SemiQuebrado",
                             tipo_suelo="Franco Arcilloso",
                             lotes=[LoteResumen(area_mz=2.5, variedad="Catuai", etapa="Productivo Selectivo",
                                                fecha_siembra=date(2019, 5, 1))])],
    )


def test_resumen_para_prompt_trae_lo_agronomico():
    texto = _perfil_registrado().resumen_para_prompt()
    assert "socio activo de Aldea Global" in texto
    assert "Café (7.4 mz)" in texto
    assert "1150 msnm" in texto
    assert "Catuai" in texto and "sembrado en 2019" in texto
    assert "orgánica" in texto
    assert "4321" not in texto  # persona_id nunca va al LLM


def test_socio_declarado_no_verificado():
    texto = PerfilUsuario(tipo_usuario="socio").resumen_para_prompt()
    assert "no verificado" in texto


def test_resumen_publico_sin_persona_id():
    publico = _perfil_registrado().resumen_publico()
    assert "persona_id" not in publico
    assert publico["cultivos"][0]["nombre"] == "Café"


def test_terreno_solo_a_productores_sin_finca():
    assert PerfilUsuario(tipo_usuario="productor").necesita_terreno()
    assert not PerfilUsuario(tipo_usuario="comerciante").necesita_terreno()
    assert not _perfil_registrado().necesita_terreno()
    assert not PerfilUsuario(tipo_usuario="productor", tipo_suelo="arcilloso").necesita_terreno()


def test_fotos_frescas():
    fechas = {"persona": date(2026, 10, 10), "finca": date(2026, 10, 9)}
    assert validar_fotos(fechas, hoy=date(2026, 10, 10)) == date(2026, 10, 9)


def test_foto_vieja_falla():
    with pytest.raises(FotoInvalidaError, match="finca"):
        validar_fotos({"persona": date(2026, 10, 10), "finca": date(2026, 10, 1)}, hoy=date(2026, 10, 10))


def test_tabla_sin_foto_falla():
    with pytest.raises(FotoInvalidaError, match="sin fotos"):
        validar_fotos({"persona": date(2026, 10, 10), "finca": None}, hoy=date(2026, 10, 10))
