# tests/test_perfilamiento.py
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.traceability import SesionPerfil
from app.services import perfilamiento
from app.services.perfil_cliente import Cultivo, FincaResumen, PerfilUsuario, hash_determinista
from app.services.perfilamiento import (
    ETAPA_PERFILAMIENTO, ETAPA_RESPUESTA, ExtraccionPerfil, ExtractorPerfil, Perfilador,
)
from app.services.prompt_config import construir_prompt_config
from tests.test_prompt_config import FILAS_VALIDAS

CFG = construir_prompt_config(FILAS_VALIDAS)
SALT = "salt-de-prueba"


class FakeDB:
    """Guarda las sesiones en memoria, como AsyncSession.get/add."""

    def __init__(self):
        self.sesiones = {}

    async def get(self, modelo, clave):
        return self.sesiones.get(clave)

    def add(self, obj):
        self.sesiones[obj.sesion_id] = obj


class FakeExtractor:
    def __init__(self, *extracciones):
        self.pendientes = list(extracciones)
        self.textos = []

    async def extraer(self, texto, cfg):
        self.textos.append(texto)
        return self.pendientes.pop(0) if self.pendientes else ExtraccionPerfil()


def _turno(perfilador, texto, sesion="s1"):
    return asyncio.run(perfilador.procesar_turno(sesion, texto, CFG))


def test_sesion_nueva_siempre_pregunta_quien_es():
    # Aunque el primer mensaje ya diga mucho, se empieza preguntando si es socio/cliente.
    ext = ExtraccionPerfil(tipo_usuario="productor", cultivos=[{"nombre": "Café", "area": 3, "unidad": "mz"}],
                           consulta_tecnica="¿Qué le echo a la roya?")
    db = FakeDB()
    r = _turno(Perfilador(db, FakeExtractor(ext), SALT), "Soy productor de café, ¿qué le echo a la roya?")
    assert r.etapa == ETAPA_PERFILAMIENTO
    assert r.respuesta == CFG.perfil_msg_bienvenida
    sesion = db.sesiones["s1"]
    assert sesion.consulta_pendiente == "¿Qué le echo a la roya?"
    assert sesion.preguntas == ["tipo_usuario"]


def test_flujo_productor_hasta_responder_la_consulta_pendiente():
    db = FakeDB()
    extractor = FakeExtractor(
        ExtraccionPerfil(consulta_tecnica="¿Qué le echo a la roya?"),
        ExtraccionPerfil(tipo_usuario="productor"),
        ExtraccionPerfil(cultivos=[{"nombre": "Café", "area": 2, "unidad": "ha"}]),
        ExtraccionPerfil(municipio="El Cuá", departamento="Jinotega"),
        ExtraccionPerfil(desconoce_terreno=True),
    )
    p = Perfilador(db, extractor, SALT)

    assert _turno(p, "Hola, ¿qué le echo a la roya?").respuesta == CFG.perfil_msg_bienvenida
    assert _turno(p, "No, soy productor").respuesta == CFG.perfil_msg_pedir_cultivo
    assert _turno(p, "2 hectáreas de café").respuesta == CFG.perfil_msg_pedir_ubicacion
    assert _turno(p, "En El Cuá, Jinotega").respuesta == CFG.perfil_msg_pedir_terreno
    r = _turno(p, "no sé")

    assert r.etapa == ETAPA_RESPUESTA
    assert r.consulta_para_rag == "¿Qué le echo a la roya?"
    assert r.perfil.cultivos == [Cultivo(nombre="Café", area_mz=2.85)]
    assert db.sesiones["s1"].estado == "completo"
    assert db.sesiones["s1"].consulta_pendiente is None


def test_sin_consulta_pendiente_pide_la_consulta():
    extractor = FakeExtractor(
        ExtraccionPerfil(tipo_usuario="comerciante", cultivos=[{"nombre": "Maíz"}], departamento="Matagalpa"),
        ExtraccionPerfil(tipo_usuario="comerciante"),
    )
    p = Perfilador(FakeDB(), extractor, SALT)
    _turno(p, "Tengo un agroservicio en Matagalpa, vendo para maíz")
    r = _turno(p, "agroservicio")
    # comerciante: no se le pregunta por el terreno
    assert r.etapa == ETAPA_PERFILAMIENTO
    assert r.respuesta == CFG.perfil_msg_completo


def test_sesion_completa_va_directo_al_rag():
    db = FakeDB()
    db.add(SesionPerfil(sesion_id="s1", estado="completo", perfil={"tipo_usuario": "productor"},
                        cedula_hashes=[], intentos_identificacion=0, preguntas_hechas=2, preguntas=[]))
    extractor = FakeExtractor()
    r = _turno(Perfilador(db, extractor, SALT), "¿Y para la broca?")
    assert r.etapa == ETAPA_RESPUESTA
    assert r.consulta_para_rag == "¿Y para la broca?"
    assert extractor.textos == []  # sin llamada al LLM de extracción


def test_tope_de_preguntas_responde_con_lo_que_hay():
    filas = dict(FILAS_VALIDAS, perfil_max_preguntas="1")
    cfg = construir_prompt_config(filas)
    p = Perfilador(FakeDB(), FakeExtractor(ExtraccionPerfil(consulta_tecnica="¿dosis de urea?")), SALT)
    asyncio.run(p.procesar_turno("s1", "¿dosis de urea?", cfg))
    r = asyncio.run(p.procesar_turno("s1", "prefiero no decir", cfg))
    assert r.etapa == ETAPA_RESPUESTA
    assert r.perfil.tipo_usuario == "otro"
    assert r.consulta_para_rag == "¿dosis de urea?"


def _registro():
    return PerfilUsuario(
        tipo_usuario="socio", fuente="registro", verificado=True, persona_id=42, es_asociado_activo=True,
        sucursal_id=7, cultivos=[Cultivo(nombre="Café", area_mz=5)],
        fincas=[FincaResumen(area_total_mz=8, municipio="Jinotega", departamento="Jinotega")],
    )


def test_socio_identificado_con_cedula_y_telefono(monkeypatch):
    buscar = AsyncMock(return_value=42)
    monkeypatch.setattr(perfilamiento, "buscar_persona_id", buscar)
    monkeypatch.setattr(perfilamiento, "cargar_perfil_registrado", AsyncMock(return_value=_registro()))
    db = FakeDB()
    extractor = FakeExtractor(ExtraccionPerfil(tipo_usuario="socio", consulta_tecnica="¿Cómo controlo la broca?"))

    r = _turno(Perfilador(db, extractor, SALT),
               "Soy socio, cédula 001-010190-0000A, WhatsApp 8888-7777. ¿Cómo controlo la broca?")

    # Con la finca registrada no hace falta preguntar nada: responde la consulta.
    assert r.etapa == ETAPA_RESPUESTA
    assert r.consulta_para_rag == "¿Cómo controlo la broca?"
    assert r.aviso.startswith("Encontré su registro: socio activo de Aldea Global")
    assert r.perfil.verificado and r.perfil.persona_id == 42
    # A la base solo llegan hashes; al LLM, el texto sin cédula ni teléfono.
    _, cedula_hashes, telefono_hashes = buscar.await_args.args
    assert hash_determinista("001-010190-0000A", SALT) in cedula_hashes
    assert telefono_hashes == [hash_determinista("88887777", SALT)]
    assert "001-010190" not in extractor.textos[0] and "8888" not in extractor.textos[0]
    assert db.sesiones["s1"].cedula_hashes == []


def test_cedula_sin_telefono_pide_el_telefono(monkeypatch):
    monkeypatch.setattr(perfilamiento, "buscar_persona_id", AsyncMock(return_value=None))
    db = FakeDB()
    r = _turno(Perfilador(db, FakeExtractor(), SALT), "mi cédula es 001-010190-0000A")
    assert r.respuesta == CFG.perfil_msg_pedir_telefono
    sesion = db.sesiones["s1"]
    assert sesion.cedula_hashes and all(len(h) == 64 for h in sesion.cedula_hashes)
    assert "001-010190-0000A" not in str(sesion.cedula_hashes)


def test_registro_no_encontrado_sigue_con_preguntas(monkeypatch):
    monkeypatch.setattr(perfilamiento, "buscar_persona_id", AsyncMock(return_value=None))
    db = FakeDB()
    p = Perfilador(db, FakeExtractor(ExtraccionPerfil(), ExtraccionPerfil(tipo_usuario="socio")), SALT)
    _turno(p, "hola")
    r = _turno(p, "soy socio, 001-010190-0000A, 88887777")
    assert r.respuesta == f"{CFG.perfil_msg_no_encontrado}\n\n{CFG.perfil_msg_pedir_cultivo}"
    assert db.sesiones["s1"].intentos_identificacion == 1


def test_sin_salt_no_identifica(monkeypatch):
    buscar = AsyncMock()
    monkeypatch.setattr(perfilamiento, "buscar_persona_id", buscar)
    r = _turno(Perfilador(FakeDB(), FakeExtractor(), None), "001-010190-0000A 88887777")
    assert r.respuesta.startswith(CFG.perfil_msg_no_encontrado)
    buscar.assert_not_awaited()


def test_extractor_tolera_fallo_del_llm():
    llm = MagicMock()
    llm.chat.completions.create = AsyncMock(side_effect=TimeoutError("lento"))
    assert asyncio.run(ExtractorPerfil(llm).extraer("hola", CFG)) == ExtraccionPerfil()


def test_extractor_valida_el_json():
    llm = MagicMock()
    contenido = ('{"tipo_usuario": "Técnico", "cultivos": [{"nombre": "Papa", "area": 1}, {"nombre": ""}],'
                 ' "desconoce_terreno": null, "otra": 1}')
    llm.chat.completions.create = AsyncMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=contenido))]))
    ext = asyncio.run(ExtractorPerfil(llm).extraer("soy técnico, 1 mz de papa", CFG))
    assert ext.tipo_usuario == "tecnico"
    assert [c.nombre for c in ext.cultivos] == ["Papa"]
    assert ext.desconoce_terreno is False


@pytest.mark.parametrize("tipo", ["agricultor", "", None])
def test_tipo_usuario_desconocido_se_descarta(tipo):
    assert ExtraccionPerfil(tipo_usuario=tipo).tipo_usuario is None


def test_desconoce_terreno_no_salta_la_pregunta_de_terreno():
    # El LLM a veces marca desconoce_terreno en mensajes que no hablan del terreno.
    extractor = FakeExtractor(
        ExtraccionPerfil(consulta_tecnica="¿Qué le aplico a la roya?", desconoce_terreno=True),
        ExtraccionPerfil(tipo_usuario="productor", desconoce_terreno=True),
        ExtraccionPerfil(cultivos=[{"nombre": "Café", "area": 3, "unidad": "mz"}]),
        ExtraccionPerfil(municipio="San Rafael del Norte", departamento="Jinotega"),
    )
    p = Perfilador(FakeDB(), extractor, SALT)
    for texto in ("¿Qué le aplico a la roya?", "soy productor", "3 mz de café"):
        _turno(p, texto)
    assert _turno(p, "San Rafael del Norte, Jinotega").respuesta == CFG.perfil_msg_pedir_terreno
