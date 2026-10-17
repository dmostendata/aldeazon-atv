# app/services/perfil_cliente.py
"""
Perfil de quien pregunta: tipo de usuario, cultivos, ubicación y terreno.

Dos fuentes, que se combinan en `PerfilUsuario`:
  - 'registro': socio/cliente de Aldea Global identificado por cédula + teléfono
    contra atv.perfil_* (copia sin PII en claro de Semillero, ver
    scripts/003_create_perfil.sql y scripts/construir_perfiles.py).
  - 'declarado': lo que el usuario contesta a las preguntas de perfilamiento.

LEY 787: la cédula y el teléfono se hashean apenas se leen del texto crudo
(HMAC-SHA256 con el salt de Semillero, igual que core/masking.py de Semillero)
y nunca se guardan ni se envían al LLM. Ni `resumen_para_prompt` ni
`resumen_publico` incluyen nombres, documentos, teléfonos ni coordenadas.
"""
import hashlib
import hmac
import logging
import re
from datetime import date
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("atv.perfil_cliente")

MZ_POR_HA = 1 / 0.7026  # 1 manzana = 0,7026 ha

# Cédula nicaragüense: 001-010190-0000A (con o sin guiones)
REGEX_CEDULA = re.compile(r'\b(\d{3})-?(\d{6})-?(\d{4})([A-Za-z])\b')
# Teléfono: 8 dígitos que empiezan con 5, 7 u 8; acepta +505 y separadores (8888-7777, 8888 7777)
REGEX_TELEFONO = re.compile(r'(?<![\d-])(?:\+?505[\s-]?)?([578]\d{3})[\s-]?(\d{4})(?![\d-])')

TIPOS_USUARIO = ("socio", "cliente", "productor", "comerciante", "tecnico", "otro")
# Tipos que no tienen "su" finca: no se les pregunta por el terreno
TIPOS_SIN_FINCA = ("comerciante", "tecnico")

MAX_FINCAS_PROMPT = 3
MAX_LOTES_PROMPT = 5


# =====================================================================
# Identificadores (hash compatible con Semillero)
# =====================================================================
def hash_determinista(valor: str, salt: str) -> str:
    """HMAC-SHA256(salt, valor) hexdigest: el mismo hash_determinista de Semillero."""
    return hmac.new(salt.encode("utf-8"), str(valor).encode("utf-8"), hashlib.sha256).hexdigest()


def extraer_cedula(texto: str) -> Optional[List[str]]:
    """
    Devuelve las variantes con que la cédula puede estar escrita en el origen
    (ssp.persona: 98,6 % '001-010190-0000A'; hay minúsculas y sin guiones), o
    None si el texto no trae una cédula. Semillero hashea el valor tal cual
    está en el origen, así que hay que probar todas.
    """
    m = REGEX_CEDULA.search(texto)
    if not m:
        return None
    a, b, c, letra = m.groups()
    variantes = []
    for l in (letra.upper(), letra.lower()):
        variantes.append(f"{a}-{b}-{c}{l}")
        variantes.append(f"{a}{b}{c}{l}")
    return variantes


def extraer_telefonos(texto: str) -> List[str]:
    """Teléfonos de 8 dígitos (sin 505 ni separadores) que aparecen en el texto, sin las cédulas."""
    sin_cedulas = REGEX_CEDULA.sub(" ", texto)
    return list(dict.fromkeys(a + b for a, b in REGEX_TELEFONO.findall(sin_cedulas)))


def ocultar_telefonos(texto: str) -> str:
    """Complementa sanitizar_pii: oculta también '8888-7777', '8888 7777' y '+505 ...'."""
    return REGEX_TELEFONO.sub("[TELEFONO]", texto)


# =====================================================================
# Modelo del perfil
# =====================================================================
class Cultivo(BaseModel):
    nombre: str
    area_mz: Optional[float] = None


class LoteResumen(BaseModel):
    nombre: Optional[str] = None
    area_mz: Optional[float] = None
    variedad: Optional[str] = None
    etapa: Optional[str] = None
    fecha_siembra: Optional[date] = None


class FincaResumen(BaseModel):
    area_total_mz: Optional[float] = None
    area_cafe_mz: Optional[float] = None
    altura_msnm: Optional[float] = None
    porc_pendiente: Optional[float] = None
    topografia: Optional[str] = None
    tipo_suelo: Optional[str] = None
    municipio: Optional[str] = None
    departamento: Optional[str] = None
    lotes: List[LoteResumen] = Field(default_factory=list)


class PerfilUsuario(BaseModel):
    """Perfil de la sesión. Se guarda como JSON en atv.sesion_perfil.perfil."""
    tipo_usuario: Optional[str] = None
    fuente: str = "declarado"            # 'declarado' | 'registro'
    verificado: bool = False
    # Clave interna para releer el registro; nunca va al prompt ni a la respuesta.
    persona_id: Optional[int] = None
    es_asociado_activo: bool = False
    tipo_cliente: Optional[str] = None
    sucursal_id: Optional[int] = None
    es_organico: Optional[bool] = None
    cultivos: List[Cultivo] = Field(default_factory=list)
    municipio: Optional[str] = None
    departamento: Optional[str] = None
    etapa_cultivo: Optional[str] = None
    topografia: Optional[str] = None
    tipo_suelo: Optional[str] = None
    altura_msnm: Optional[float] = None
    tipo_negocio: Optional[str] = None
    terreno_preguntado: bool = False
    fincas: List[FincaResumen] = Field(default_factory=list)

    # --- completitud -------------------------------------------------
    def tiene_ubicacion(self) -> bool:
        return bool(self.municipio or self.departamento or any(
            f.municipio or f.departamento for f in self.fincas))

    def necesita_terreno(self) -> bool:
        """Etapa/terreno es opcional: se pregunta una vez a productores sin finca registrada."""
        if self.terreno_preguntado or self.fincas or self.tipo_usuario in TIPOS_SIN_FINCA:
            return False
        return not (self.etapa_cultivo or self.topografia or self.tipo_suelo or self.altura_msnm)

    def cultivo_principal(self) -> Optional[str]:
        return self.cultivos[0].nombre if self.cultivos else None

    # --- textos sin PII ----------------------------------------------
    def _quien(self) -> str:
        if self.tipo_usuario in ("socio", "cliente") and not self.verificado:
            return f"dice ser {self.tipo_usuario} de Aldea Global (no verificado)"
        if self.tipo_usuario == "socio":
            return "socio activo de Aldea Global" if self.es_asociado_activo else "socio de Aldea Global (no activo)"
        if self.tipo_usuario == "cliente":
            detalle = f" ({self.tipo_cliente})" if self.tipo_cliente else ""
            return f"cliente de Aldea Global{detalle}"
        if self.tipo_usuario == "comerciante":
            return "comerciante / agroservicio" + (f" ({self.tipo_negocio})" if self.tipo_negocio else "")
        if self.tipo_usuario == "tecnico":
            return "técnico agrícola"
        if self.tipo_usuario == "productor":
            return "productor (no registrado en Aldea Global)"
        return self.tipo_usuario or "sin identificar"

    def _cultivos_txt(self) -> str:
        partes = []
        for c in self.cultivos:
            partes.append(f"{c.nombre} ({_num(c.area_mz)} mz)" if c.area_mz else c.nombre)
        return ", ".join(partes)

    def _ubicacion_txt(self) -> Optional[str]:
        mun = self.municipio or next((f.municipio for f in self.fincas if f.municipio), None)
        dep = self.departamento or next((f.departamento for f in self.fincas if f.departamento), None)
        return ", ".join(dict.fromkeys(x for x in (mun, dep) if x)) or None

    def resumen_para_prompt(self) -> str:
        """Bloque {perfil} del prompt de generación."""
        lineas = [f"- Quién pregunta: {self._quien()}"]
        if self.cultivos:
            lineas.append(f"- Cultivos: {self._cultivos_txt()}")
        ubicacion = self._ubicacion_txt()
        if ubicacion:
            lineas.append(f"- Ubicación: {ubicacion}")
        if self.es_organico:
            lineas.append("- Producción orgánica: sí (priorizar productos permitidos en agricultura orgánica)")
        if self.etapa_cultivo:
            lineas.append(f"- Etapa del cultivo: {self.etapa_cultivo}")
        terreno = [x for x in (
            self.topografia and f"topografía {self.topografia}",
            self.tipo_suelo and f"suelo {self.tipo_suelo}",
            self.altura_msnm and f"{_num(self.altura_msnm)} msnm",
        ) if x]
        if terreno:
            lineas.append(f"- Terreno: {', '.join(terreno)}")
        for i, f in enumerate(self.fincas[:MAX_FINCAS_PROMPT], start=1):
            datos = [x for x in (
                f.area_total_mz and f"{_num(f.area_total_mz)} mz en total",
                f.area_cafe_mz and f"{_num(f.area_cafe_mz)} mz de café",
                f.altura_msnm and f"{_num(f.altura_msnm)} msnm",
                f.porc_pendiente and f"pendiente {_num(f.porc_pendiente)} %",
                f.topografia and f"topografía {f.topografia}",
                f.tipo_suelo and f"suelo {f.tipo_suelo}",
            ) if x]
            lineas.append(f"- Finca {i}: " + (", ".join(datos) or "sin datos de terreno"))
            for lote in f.lotes[:MAX_LOTES_PROMPT]:
                d = [x for x in (
                    lote.area_mz and f"{_num(lote.area_mz)} mz",
                    lote.variedad and f"variedad {lote.variedad}",
                    lote.etapa and f"etapa {lote.etapa}",
                    lote.fecha_siembra and f"sembrado en {lote.fecha_siembra.year}",
                ) if x]
                if d:
                    lineas.append(f"  - Lote: {', '.join(d)}")
        return "\n".join(lineas)

    def resumen_corto(self) -> str:
        """Para perfil_msg_verificado: lo que se le confirma al usuario identificado."""
        partes = [self._quien()]
        if self.cultivos:
            partes.append(f"cultivos: {self._cultivos_txt()}")
        if self.fincas:
            partes.append(f"{len(self.fincas)} finca(s) registrada(s)")
        ubicacion = self._ubicacion_txt()
        if ubicacion:
            partes.append(f"en {ubicacion}")
        return "; ".join(partes)

    def resumen_publico(self) -> Dict[str, Any]:
        """Lo que se devuelve a la web: sin persona_id ni datos personales."""
        return self.model_dump(exclude={"persona_id", "terreno_preguntado"}, mode="json")


def _num(valor: Optional[float]) -> str:
    return f"{valor:.1f}".rstrip("0").rstrip(".") if valor is not None else ""


def area_a_mz(area: Optional[float], unidad: Optional[str]) -> Optional[float]:
    if area is None or area <= 0:
        return None
    return round(area * MZ_POR_HA, 2) if (unidad or "").lower() == "ha" else round(area, 2)


# =====================================================================
# Lectura de atv.perfil_* (registro de socios/clientes)
# =====================================================================
def hashes(valores: List[str], salt: str) -> List[str]:
    return [hash_determinista(v, salt) for v in valores]


async def buscar_persona_id(
    session: AsyncSession, cedula_hashes: List[str], telefono_hashes: List[str]
) -> Optional[int]:
    """
    persona_id del socio/cliente cuya cédula Y teléfono coinciden, o None.
    Pedir los dos evita que alguien vea la finca de otro con solo saber su cédula.
    """
    if not cedula_hashes or not telefono_hashes:
        return None
    resultado = await session.execute(
        text(
            "SELECT persona_id FROM atv.perfil_productor "
            "WHERE cedula_hash = ANY(CAST(:cedulas AS text[])) "
            "AND telefono_hashes && CAST(:telefonos AS text[]) "
            "ORDER BY es_asociado DESC, persona_id LIMIT 1"
        ),
        {"cedulas": cedula_hashes, "telefonos": telefono_hashes},
    )
    return resultado.scalar()


async def cargar_perfil_registrado(session: AsyncSession, persona_id: int) -> Optional[PerfilUsuario]:
    fila = (await session.execute(
        text(
            "SELECT es_asociado, estado_asociado, tipo_cliente, sucursal_id, es_organico, "
            "comunidad, municipio, departamento, rubros "
            "FROM atv.perfil_productor WHERE persona_id = :pid"
        ),
        {"pid": persona_id},
    )).mappings().first()
    if fila is None:
        return None

    fincas_rows = (await session.execute(
        text(
            "SELECT finca_id, area_total_mz, area_cafe_mz, altura_msnm, porc_pendiente, topografia, "
            "tipo_suelo, municipio, departamento FROM atv.perfil_finca "
            "WHERE persona_id = :pid ORDER BY area_total_mz DESC NULLS LAST, finca_id"
        ),
        {"pid": persona_id},
    )).mappings().all()

    lotes_por_finca: Dict[int, List[LoteResumen]] = {}
    if fincas_rows:
        lotes_rows = (await session.execute(
            text(
                "SELECT finca_id, nombre, area_mz, variedad, etapa, fecha_siembra FROM atv.perfil_lote "
                "WHERE finca_id = ANY(CAST(:fincas AS integer[])) ORDER BY area_mz DESC NULLS LAST, lote_id"
            ),
            {"fincas": [f["finca_id"] for f in fincas_rows]},
        )).mappings().all()
        for l in lotes_rows:
            lotes_por_finca.setdefault(l["finca_id"], []).append(LoteResumen(
                nombre=l["nombre"], area_mz=_float(l["area_mz"]), variedad=l["variedad"],
                etapa=l["etapa"], fecha_siembra=l["fecha_siembra"],
            ))

    fincas = [
        FincaResumen(
            area_total_mz=_float(f["area_total_mz"]), area_cafe_mz=_float(f["area_cafe_mz"]),
            altura_msnm=_float(f["altura_msnm"]), porc_pendiente=_float(f["porc_pendiente"]),
            topografia=f["topografia"], tipo_suelo=f["tipo_suelo"],
            municipio=f["municipio"], departamento=f["departamento"],
            lotes=lotes_por_finca.get(f["finca_id"], []),
        )
        for f in fincas_rows
    ]
    cultivos = [
        Cultivo(nombre=r["rubro"], area_mz=_float(r.get("mz")) or None)
        for r in (fila["rubros"] or []) if r.get("rubro")
    ]
    estado = (fila["estado_asociado"] or "").lower()
    return PerfilUsuario(
        tipo_usuario="socio" if fila["es_asociado"] else "cliente",
        fuente="registro",
        verificado=True,
        persona_id=persona_id,
        es_asociado_activo=bool(fila["es_asociado"]) and estado == "activo",
        tipo_cliente=fila["tipo_cliente"],
        sucursal_id=fila["sucursal_id"],
        es_organico=fila["es_organico"],
        cultivos=cultivos,
        municipio=fila["municipio"],
        departamento=fila["departamento"],
        fincas=fincas,
    )


def _float(valor: Any) -> Optional[float]:
    return float(valor) if valor is not None else None
