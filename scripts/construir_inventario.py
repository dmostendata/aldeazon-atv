"""
Reconstruye atv.inventario_producto (scripts/011_create_inventario.sql) desde la
última foto de stg_insumos (Semillero, catálogo
scripts/semillero/005_insumos_inventario.sql).

Lo invoca el DAG 'atv_perfil_productor' (dags/dag_perfil_productor.py) desde
VPS2 vía `python -m scripts.airflow_tasks construir_inventario`.

Ingredientes activos de cada producto: stg_insumos.ingrediente_activo +
agroquimico_ingrediente_activo (catálogo Semillero 006).

Misma lógica que la vista stc2.vw_az_productos_inventario_ia (existe solo en
develop): inventario JOIN producto JOIN bodega JOIN unidad JOIN clasificación,
bodega activa, producto activo, cantidad_real >= 0, sin "picadora(s)". Además
deja fuera las bodegas que no son tiendas del cliente: internacionales y
"Producto Vencido".

Dos bases, como construir_perfiles.py: lee `semillero` (SEMILLERO_PG_*) y
escribe `staging` (DATABASE_URL) en una sola transacción.

Gates (fallan la tarea SIN tocar el inventario vigente):
  - alguna de las 5 tablas sin fotos o con la última foto de hace más de
    MAX_DIAS_FOTO días;
  - el nuevo inventario tiene menos de MIN_PROPORCION_FILAS de las filas del vigente.
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
from app.services.producto_service import claves_ingrediente, nombre_corto_producto

logger = logging.getLogger("atv.construir_inventario")

ESQUEMA_ORIGEN = "stg_insumos"
# Las unidades ya las carga el catálogo de credito (stg_credito): un origen no puede
# declararse dos veces en Semillero (ver scripts/semillero/005_insumos_inventario.sql).
TABLAS_ORIGEN = (("stg_insumos", "az_bodega_inventario"), ("stg_insumos", "agroquimico"),
                 ("stg_insumos", "az_catalogo_bodegas"), ("stg_insumos", "agroquimico_clasificacion"),
                 ("stg_insumos", "ingrediente_activo"), ("stg_insumos", "agroquimico_ingrediente_activo"),
                 ("stg_credito", "cre_catalogo_unidad_medida"))
MAX_DIAS_FOTO = 2
MIN_PROPORCION_FILAS = 0.5
BODEGAS_EXCLUIDAS = ("producto vencido",)


class FotoInvalidaError(RuntimeError):
    """Las fotos de stg_insumos no sirven para reconstruir el inventario."""


def _ultima(tabla: str, esquema: str = ESQUEMA_ORIGEN) -> str:
    return (f"(SELECT * FROM {esquema}.{tabla} WHERE _fecha_carga = "
            f"(SELECT max(_fecha_carga) FROM {esquema}.{tabla}))")


def _bool(expr: str) -> str:
    # Semillero guarda tinyint(1) como integer; comparar el texto cubre también boolean.
    return f"(({expr})::text IN ('true', '1'))"


SQL_INVENTARIO = f"""
SELECT a.id AS producto_id, b.id AS bodega_id, a.nombre AS producto_nombre,
       b.nombre AS bodega_nombre, b.id_sucursal AS sucursal_id,
       um.unidad AS unidad, ac.nombre AS clasificacion,
       i.cantidad_disponible, i.cantidad_real,
       coalesce(ing.nombres, ARRAY[]::text[]) AS ingredientes
  FROM {_ultima('az_bodega_inventario')} i
  JOIN {_ultima('agroquimico')} a ON a.id = i.agroquimico_id
  JOIN {_ultima('az_catalogo_bodegas')} b ON b.id = i.az_catalogo_bodegas_id
  JOIN {_ultima('cre_catalogo_unidad_medida', 'stg_credito')} um ON um.id = a.cre_catalogo_unidad_medida_id
  JOIN {_ultima('agroquimico_clasificacion')} ac ON ac.id = a.agroquimico_clasificacion_id
  LEFT JOIN (SELECT x.agroquimico_id, array_agg(DISTINCT ia.nombre::text) AS nombres
               FROM {_ultima('agroquimico_ingrediente_activo')} x
               JOIN {_ultima('ingrediente_activo')} ia ON ia.id = x.ingrediente_activo_id
              GROUP BY x.agroquimico_id) ing ON ing.agroquimico_id = a.id
 WHERE {_bool('b.activo')} AND {_bool('a.estado')}
   AND i.cantidad_real >= 0
   AND lower(a.nombre) NOT LIKE '%picadora%'
   AND NOT coalesce({_bool('b.es_internacional')}, false)
   AND lower(b.nombre) NOT IN ({", ".join(f"'{n}'" for n in BODEGAS_EXCLUIDAS)})
"""

INSERT_INVENTARIO = """
INSERT INTO atv.inventario_producto
    (producto_id, bodega_id, producto_nombre, nombre_clave, bodega_nombre, sucursal_id,
     unidad, clasificacion, cantidad_disponible, cantidad_real, fecha_foto, ingredientes, ingredientes_clave)
VALUES (:producto_id, :bodega_id, :producto_nombre, :nombre_clave, :bodega_nombre, :sucursal_id,
        :unidad, :clasificacion, :cantidad_disponible, :cantidad_real, :fecha_foto, :ingredientes, :ingredientes_clave)
"""


def validar_fotos(fechas: Dict[str, date], hoy: date, max_dias: int = MAX_DIAS_FOTO) -> date:
    """Devuelve la fecha de foto del inventario (la más vieja de las tablas usadas)."""
    sin_foto = sorted(t for t, f in fechas.items() if f is None)
    if sin_foto:
        raise FotoInvalidaError(f"Tablas de Semillero sin fotos: {sin_foto}")
    limite = hoy - timedelta(days=max_dias)
    viejas = sorted(f"{t} ({f})" for t, f in fechas.items() if f < limite)
    if viejas:
        raise FotoInvalidaError(
            f"Fotos de Semillero anteriores a {limite}: {viejas}. Revisar los DAGs "
            "semillero_ingesta_insumos_inventario_01 / credito catalogos antes de reconstruir el inventario."
        )
    return min(fechas.values())


async def leer_de_semillero(url: str, hoy: date) -> tuple[date, List[dict]]:
    engine = create_async_engine(url, pool_size=1, max_overflow=0)
    try:
        async with engine.connect() as conn:
            fechas = {
                f"{esquema}.{tabla}": (await conn.execute(text(f"SELECT max(_fecha_carga) FROM {esquema}.{tabla}"))).scalar()
                for esquema, tabla in TABLAS_ORIGEN
            }
            fecha_foto = validar_fotos(fechas, hoy)
            filas = [dict(r) for r in (await conn.execute(text(SQL_INVENTARIO))).mappings()]
    finally:
        await engine.dispose()
    for f in filas:
        f["fecha_foto"] = fecha_foto
        f["nombre_clave"] = nombre_corto_producto(f["producto_nombre"])
        # ingredientes_clave en el mismo orden que ingredientes ("Tebuconazole" -> "tebuconasol")
        f["ingredientes"] = list(f["ingredientes"] or [])
        f["ingredientes_clave"] = [" ".join(claves_ingrediente(n)) for n in f["ingredientes"]]
    return fecha_foto, filas


async def construir_inventario(hoy: date | None = None) -> Dict[str, int]:
    if not settings.semillero_database_url:
        raise RuntimeError("Faltan SEMILLERO_PG_USER / SEMILLERO_PG_PASSWORD en .env (base `semillero`, solo lectura de stg_insumos y stg_credito.cre_catalogo_unidad_medida).")
    fecha_foto, filas = await leer_de_semillero(settings.semillero_database_url, hoy or date.today())

    async with AsyncSessionLocal() as db:
        vigentes = (await db.execute(text("SELECT count(*) FROM atv.inventario_producto"))).scalar()
        if vigentes and len(filas) < vigentes * MIN_PROPORCION_FILAS:
            raise FotoInvalidaError(
                f"inventario_producto bajaría de {vigentes} a {len(filas)} filas "
                f"(< {MIN_PROPORCION_FILAS:.0%}). Se conserva el inventario vigente."
            )
        # Una sola transacción: la API sigue leyendo el inventario vigente hasta el COMMIT.
        await db.execute(text("DELETE FROM atv.inventario_producto"))
        if filas:
            await db.execute(text(INSERT_INVENTARIO), filas)
        await db.commit()

    conteos = {"inventario_producto": len(filas),
               "con_existencias": sum(1 for f in filas if (f["cantidad_disponible"] or 0) > 0),
               "productos": len({f["producto_id"] for f in filas})}
    logger.info(f"Inventario reconstruido con la foto {fecha_foto}: {conteos}")
    return conteos


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    print(json.dumps(asyncio.run(construir_inventario())))
