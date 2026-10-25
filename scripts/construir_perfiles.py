"""
Reconstruye atv.perfil_productor / perfil_finca / perfil_lote desde la última
foto de stg_cafe (Semillero, catálogo 004: scripts/semillero/004_cafe_perfil_finca.sql).

Lo invoca el DAG 'atv_perfil_productor' (dags/dag_perfil_productor.py) desde
VPS2 vía `python -m scripts.airflow_tasks construir_perfiles`.

Dos bases en el mismo PostgreSQL (10.0.0.14):
  - SEMILLERO_PG_* en .env (base `semillero`): stg_cafe, donde Semillero deja
    las fotos. Solo lectura.
  - DATABASE_URL (base `staging`): atv.perfil_*, lo que lee la API.
PostgreSQL no cruza bases en una consulta: se lee de una y se escribe en la otra.

Semillero guarda una foto completa por día (_fecha_carga) y nunca borra las
viejas: acá se toma siempre la última de cada tabla.

Gates (fallan la tarea SIN tocar los perfiles vigentes):
  - una tabla de origen sin fotos, o con su última foto más vieja que
    MAX_DIAS_FOTO (Semillero dejó de cargarla);
  - el nuevo perfil_productor tiene menos de MIN_PROPORCION_FILAS de las filas
    del vigente (foto incompleta).
"""

import asyncio
import json
import logging
from datetime import date, timedelta
from typing import Dict, List

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.db.session import AsyncSessionLocal

logger = logging.getLogger("atv.construir_perfiles")

ESQUEMA_ORIGEN = "stg_cafe"
TABLAS_ORIGEN = (
    "persona", "asociado", "cliente", "comunidad", "distrito", "municipio", "ssp_departamento",
    "asociado_mz_rubro", "finca", "finca_lote", "finca_lote_estado", "catalogo_variedad_cafe",
)
MAX_DIAS_FOTO = 2
MIN_PROPORCION_FILAS = 0.5


class FotoInvalidaError(RuntimeError):
    """Las fotos de staging no sirven para reconstruir el perfil."""


def _ultima(tabla: str) -> str:
    return (f"(SELECT * FROM {ESQUEMA_ORIGEN}.{tabla} WHERE _fecha_carga = "
            f"(SELECT max(_fecha_carga) FROM {ESQUEMA_ORIGEN}.{tabla}))")


def _bool(expr: str) -> str:
    # Semillero guarda tinyint(1) como integer (core/ddl_generator.py); comparar
    # el texto cubre también una columna boolean.
    return f"(({expr})::text IN ('true', '1'))"


_UBICACION = f"""
LEFT JOIN {_ultima('distrito')} di ON di.id = co.distrito_id
LEFT JOIN {_ultima('municipio')} mu ON mu.id = di.municipio_id
LEFT JOIN {_ultima('ssp_departamento')} de ON de.id = mu.departamento_id"""

# --- Lecturas en la base de Semillero ------------------------------------
SQL_PRODUCTOR = f"""
SELECT p.id AS persona_id,
       p.cedula AS cedula_hash,
       array_remove(ARRAY[p.whatsapp_claro::text, p.whatsapp_tigo::text, c.celular::text], NULL) AS telefono_hashes,
       a.persona_id IS NOT NULL AS es_asociado,
       a.estado_asociado,
       c.tipo_cliente,
       coalesce(a.sucursal_id, p.sucursal_id) AS sucursal_id,
       {_bool('a.es_organico')} AS es_organico,
       coalesce({_bool('a.cafetalero')}, {_bool('p.cafetalero')}) AS cafetalero,
       a.numero_mz,
       co.nombre AS comunidad, mu.nombre AS municipio, de.nombre AS departamento,
       coalesce(rb.rubros, '[]'::jsonb)::text AS rubros
FROM {_ultima('persona')} p
LEFT JOIN {_ultima('asociado')} a ON a.persona_id = p.id
LEFT JOIN {_ultima('cliente')} c ON c.id = p.id
LEFT JOIN {_ultima('comunidad')} co ON co.id = p.comunidad_id{_UBICACION}
LEFT JOIN (
    SELECT asociado_id,
           jsonb_agg(jsonb_build_object('rubro', rubro, 'mz', round(mz_avg::numeric, 2))
                     ORDER BY mz_avg DESC NULLS LAST) AS rubros
    FROM {_ultima('asociado_mz_rubro')} r
    GROUP BY asociado_id
) rb ON rb.asociado_id = p.id
"""

SQL_FINCA = f"""
SELECT f.id AS finca_id, f.asociado_persona_id AS persona_id,
       nullif(f.altura, 0) AS altura_msnm, nullif(f.porc_pendiente, 0) AS porc_pendiente,
       nullif(trim(f.topografia), '') AS topografia, nullif(trim(f.tipo_suelo), '') AS tipo_suelo,
       f.total_area_finca AS area_total_mz, f.area_cafe_total AS area_cafe_mz,
       f.area_cafe_cosechero AS area_cafe_cosecha_mz, f.area_cafe_desarollo AS area_cafe_desarrollo_mz,
       f.area_cultivos_anuales_granos_basicos AS area_granos_basicos_mz,
       f.area_cultivos_anuales_frutas AS area_frutas_mz,
       coalesce(f.area_pasto_mejorado, 0) + coalesce(f.area_potreros, 0) AS area_pastos_mz,
       f.area_bosques AS area_bosque_mz,
       mu.nombre AS municipio, de.nombre AS departamento, f.fecha_floracion
FROM {_ultima('finca')} f
JOIN {_ultima('persona')} p ON p.id = f.asociado_persona_id
LEFT JOIN {_ultima('comunidad')} co ON co.id = f.comunidad_id{_UBICACION}
WHERE {_bool('f.activo')}
"""

SQL_LOTE = f"""
SELECT l.id AS lote_id, l.finca_id, l.nombre, l.area AS area_mz, v.nombre AS variedad, e.nombre AS etapa,
       l.fecha_siembra, l.plantas_x_mz, l.rendimiento_esperado
FROM {_ultima('finca_lote')} l
JOIN {_ultima('finca')} f ON f.id = l.finca_id AND {_bool('f.activo')}
JOIN {_ultima('persona')} p ON p.id = f.asociado_persona_id
LEFT JOIN {_ultima('catalogo_variedad_cafe')} v ON v.id = l.variedad_id
LEFT JOIN {_ultima('finca_lote_estado')} e ON e.id = l.estado_id
WHERE {_bool('l.activo')}
"""

# --- Escrituras en la base del ATV ---------------------------------------
INSERT_PRODUCTOR = """
INSERT INTO atv.perfil_productor
    (persona_id, cedula_hash, telefono_hashes, es_asociado, estado_asociado, tipo_cliente, sucursal_id,
     es_organico, cafetalero, numero_mz, comunidad, municipio, departamento, rubros, fecha_foto)
VALUES (:persona_id, :cedula_hash, :telefono_hashes, :es_asociado, :estado_asociado, :tipo_cliente, :sucursal_id,
        :es_organico, :cafetalero, :numero_mz, :comunidad, :municipio, :departamento, CAST(:rubros AS jsonb), :fecha_foto)
"""
INSERT_FINCA = """
INSERT INTO atv.perfil_finca
    (finca_id, persona_id, altura_msnm, porc_pendiente, topografia, tipo_suelo, area_total_mz, area_cafe_mz,
     area_cafe_cosecha_mz, area_cafe_desarrollo_mz, area_granos_basicos_mz, area_frutas_mz, area_pastos_mz,
     area_bosque_mz, municipio, departamento, fecha_floracion)
VALUES (:finca_id, :persona_id, :altura_msnm, :porc_pendiente, :topografia, :tipo_suelo, :area_total_mz, :area_cafe_mz,
        :area_cafe_cosecha_mz, :area_cafe_desarrollo_mz, :area_granos_basicos_mz, :area_frutas_mz, :area_pastos_mz,
        :area_bosque_mz, :municipio, :departamento, :fecha_floracion)
"""
INSERT_LOTE = """
INSERT INTO atv.perfil_lote
    (lote_id, finca_id, nombre, area_mz, variedad, etapa, fecha_siembra, plantas_x_mz, rendimiento_esperado)
VALUES (:lote_id, :finca_id, :nombre, :area_mz, :variedad, :etapa, :fecha_siembra, :plantas_x_mz, :rendimiento_esperado)
"""


def validar_fotos(fechas: Dict[str, date], hoy: date, max_dias: int = MAX_DIAS_FOTO) -> date:
    """Devuelve la fecha de foto del perfil (la más vieja de las tablas usadas)."""
    sin_foto = sorted(t for t, f in fechas.items() if f is None)
    if sin_foto:
        raise FotoInvalidaError(f"Tablas de {ESQUEMA_ORIGEN} sin fotos: {sin_foto}")
    limite = hoy - timedelta(days=max_dias)
    viejas = sorted(f"{t} ({f})" for t, f in fechas.items() if f < limite)
    if viejas:
        raise FotoInvalidaError(
            f"Fotos de {ESQUEMA_ORIGEN} anteriores a {limite}: {viejas}. "
            "Revisar los DAGs semillero_ingesta_cafe_* antes de reconstruir el perfil."
        )
    return min(fechas.values())


async def leer_de_semillero(url: str, hoy: date) -> tuple[date, List[dict], List[dict], List[dict]]:
    engine = create_async_engine(url, pool_size=1, max_overflow=0)
    try:
        async with engine.connect() as conn:
            fechas = {
                tabla: (await conn.execute(text(f"SELECT max(_fecha_carga) FROM {ESQUEMA_ORIGEN}.{tabla}"))).scalar()
                for tabla in TABLAS_ORIGEN
            }
            fecha_foto = validar_fotos(fechas, hoy)
            productores = [dict(r) for r in (await conn.execute(text(SQL_PRODUCTOR))).mappings()]
            fincas = [dict(r) for r in (await conn.execute(text(SQL_FINCA))).mappings()]
            lotes = [dict(r) for r in (await conn.execute(text(SQL_LOTE))).mappings()]
    finally:
        await engine.dispose()
    for p in productores:
        p["fecha_foto"] = fecha_foto
        p["telefono_hashes"] = list(p["telefono_hashes"] or [])
    return fecha_foto, productores, fincas, lotes


async def construir_perfiles(hoy: date | None = None) -> Dict[str, int]:
    if not settings.semillero_database_url:
        raise RuntimeError("Faltan SEMILLERO_PG_USER / SEMILLERO_PG_PASSWORD en .env (base `semillero`, solo lectura de stg_cafe).")
    fecha_foto, productores, fincas, lotes = await leer_de_semillero(
        settings.semillero_database_url, hoy or date.today()
    )
    # Solo fincas de personas que entran al perfil, y lotes de esas fincas.
    ids_finca = {f["finca_id"] for f in fincas}
    lotes = [l for l in lotes if l["finca_id"] in ids_finca]

    async with AsyncSessionLocal() as db:
        filas_vigentes = (await db.execute(text("SELECT count(*) FROM atv.perfil_productor WHERE persona_id > 0"))).scalar()
        if filas_vigentes and len(productores) < filas_vigentes * MIN_PROPORCION_FILAS:
            raise FotoInvalidaError(
                f"perfil_productor bajaría de {filas_vigentes} a {len(productores)} filas "
                f"(< {MIN_PROPORCION_FILAS:.0%}). Se conservan los perfiles vigentes."
            )

        # Una sola transacción: la API sigue leyendo los perfiles vigentes
        # (DELETE, no TRUNCATE: no bloquea lecturas) hasta el COMMIT.
        # Los ids negativos son los socios de prueba (scripts/sembrar_perfiles_prueba.py):
        # no vienen de Semillero y se conservan.
        await db.execute(text("DELETE FROM atv.perfil_lote WHERE lote_id > 0"))
        await db.execute(text("DELETE FROM atv.perfil_finca WHERE finca_id > 0"))
        await db.execute(text("DELETE FROM atv.perfil_productor WHERE persona_id > 0"))
        if productores:
            await db.execute(text(INSERT_PRODUCTOR), productores)
        if fincas:
            await db.execute(text(INSERT_FINCA), fincas)
        if lotes:
            await db.execute(text(INSERT_LOTE), lotes)
        await db.commit()

    conteos = {"perfil_productor": len(productores), "perfil_finca": len(fincas), "perfil_lote": len(lotes)}
    logger.info(f"Perfiles reconstruidos con la foto {fecha_foto}: {conteos}")
    return conteos


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    print(json.dumps(asyncio.run(construir_perfiles())))
