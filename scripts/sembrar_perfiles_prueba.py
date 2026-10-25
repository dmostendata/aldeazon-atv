"""
Socios de PRUEBA para el ATV (atv.perfil_productor / perfil_finca / perfil_lote).

    python -m scripts.sembrar_perfiles_prueba            # crea o rehace los 5
    python -m scripts.sembrar_perfiles_prueba --borrar   # los quita

Para probar la identificación de socio sin usar datos de personas reales:
  * persona_id, finca_id y lote_id NEGATIVOS: no chocan con Semillero y
    construir_perfiles.py no los borra en la reconstrucción nocturna.
  * cédulas con código 999 (no existe ese municipio) y teléfonos 5555-xxxx.
  * se guardan como en producción: HMAC-SHA256 con SEMILLERO_HASH_SALT, así el
    flujo de identificación es el mismo que con un socio real.

Cada socio cubre un caso distinto de recomendación (ver PERFILES).
"""

import argparse
import asyncio
import json
import logging
from datetime import date

from sqlalchemy import text

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.services.perfil_cliente import hash_determinista

logger = logging.getLogger("atv.sembrar_perfiles_prueba")

# cédula, WhatsApp, perfil, finca, lotes; pregunta sugerida para terminar en un producto AldeaZON
PERFILES = [
    dict(persona_id=-1, cedula="999-010180-0001A", telefono="55550001", pregunta="¿Qué tiene para la roya?",
         estado_asociado="activo", tipo_cliente="productor", sucursal_id=1, es_organico=False, cafetalero=True,
         numero_mz=8, comunidad="La Fundadora", municipio="Jinotega", departamento="Jinotega",
         rubros=[{"rubro": "Café", "mz": 8.0}],
         finca=dict(finca_id=-1, altura_msnm=1150, porc_pendiente=35, topografia="SemiQuebrado", tipo_suelo="Franco Acilloso",
                    area_total_mz=10, area_cafe_mz=8, area_cafe_cosecha_mz=7, area_cafe_desarrollo_mz=1, area_bosque_mz=2),
         lotes=[dict(lote_id=-1, nombre="Lote El Mirador", area_mz=7, variedad="Catimor", etapa="Productivo Selectivo",
                     fecha_siembra=date(2018, 6, 1), plantas_x_mz=3500, rendimiento_esperado=25)]),
    dict(persona_id=-2, cedula="999-020285-0002B", telefono="55550002", pregunta="¿Qué le echo al café para que florezca?",
         estado_asociado="activo", tipo_cliente="productor", sucursal_id=9, es_organico=True, cafetalero=True,
         numero_mz=5, comunidad="San Antonio", municipio="El Cuá", departamento="Jinotega",
         rubros=[{"rubro": "Café", "mz": 5.0}],
         finca=dict(finca_id=-2, altura_msnm=1250, porc_pendiente=45, topografia="Irregular", tipo_suelo="Arcilloso",
                    area_total_mz=6, area_cafe_mz=5, area_cafe_cosecha_mz=5, area_bosque_mz=1),
         lotes=[dict(lote_id=-2, nombre="Lote La Quebrada", area_mz=5, variedad="Caturra", etapa="Tercer Año Productivo Poda",
                     fecha_siembra=date(2016, 7, 1), plantas_x_mz=4000, rendimiento_esperado=20)]),
    dict(persona_id=-3, cedula="999-030390-0003C", telefono="55550003", pregunta="¿Qué hay para gusano cogollero en el maíz?",
         estado_asociado="activo", tipo_cliente="productor", sucursal_id=5, es_organico=False, cafetalero=False,
         numero_mz=6, comunidad="El Carrizo", municipio="Santa María de Pantasma", departamento="Jinotega",
         rubros=[{"rubro": "Maíz", "mz": 4.0}, {"rubro": "Frijol", "mz": 2.0}],
         finca=dict(finca_id=-3, altura_msnm=650, porc_pendiente=10, topografia="SemiPlano", tipo_suelo="Arcilloso Arenoso",
                    area_total_mz=7, area_granos_basicos_mz=6),
         lotes=[]),
    dict(persona_id=-4, cedula="999-040495-0004D", telefono="55550004", pregunta="Tengo mosca blanca en el tomate, ¿qué le aplico?",
         estado_asociado="activo", tipo_cliente="productor", sucursal_id=3, es_organico=False, cafetalero=False,
         numero_mz=2, comunidad="El Robledal", municipio="San Rafael del Norte", departamento="Jinotega",
         rubros=[{"rubro": "Tomate", "mz": 1.5}, {"rubro": "Chiltoma", "mz": 0.5}],
         finca=dict(finca_id=-4, altura_msnm=1000, porc_pendiente=8, topografia="Regular", tipo_suelo="Arcilloso Limoso",
                    area_total_mz=2.5, area_frutas_mz=0.5),
         lotes=[]),
    dict(persona_id=-5, cedula="999-050575-0005E", telefono="55550005", pregunta="¿Qué semilla de pasto tiene para ganado de leche?",
         estado_asociado="activo", tipo_cliente="productor", sucursal_id=10, es_organico=False, cafetalero=False,
         numero_mz=20, comunidad="Plan de Grama", municipio="Wiwilí de Jinotega", departamento="Jinotega",
         rubros=[{"rubro": "Ganado", "mz": 15.0}, {"rubro": "Maíz", "mz": 3.0}],
         finca=dict(finca_id=-5, altura_msnm=400, porc_pendiente=15, topografia="SemiQuebrado", tipo_suelo="Franco Acilloso",
                    area_total_mz=20, area_pastos_mz=15, area_granos_basicos_mz=3, area_bosque_mz=2),
         lotes=[]),
]

COLS_FINCA = ("finca_id", "persona_id", "altura_msnm", "porc_pendiente", "topografia", "tipo_suelo", "area_total_mz",
              "area_cafe_mz", "area_cafe_cosecha_mz", "area_cafe_desarrollo_mz", "area_granos_basicos_mz",
              "area_frutas_mz", "area_pastos_mz", "area_bosque_mz", "municipio", "departamento", "fecha_floracion")
COLS_LOTE = ("lote_id", "finca_id", "nombre", "area_mz", "variedad", "etapa", "fecha_siembra", "plantas_x_mz",
             "rendimiento_esperado")


def _insert(tabla: str, cols) -> str:
    return f"INSERT INTO atv.{tabla} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"


async def borrar(db) -> None:
    await db.execute(text("DELETE FROM atv.perfil_lote WHERE lote_id < 0"))
    await db.execute(text("DELETE FROM atv.perfil_finca WHERE finca_id < 0"))
    await db.execute(text("DELETE FROM atv.perfil_productor WHERE persona_id < 0"))


async def sembrar() -> int:
    salt = settings.semillero_hash_salt
    if not salt:
        raise RuntimeError("Falta SEMILLERO_HASH_SALT en .env: sin él la identificación no coincide.")
    async with AsyncSessionLocal() as db:
        await borrar(db)
        for p in PERFILES:
            await db.execute(text("""
                INSERT INTO atv.perfil_productor
                    (persona_id, cedula_hash, telefono_hashes, es_asociado, estado_asociado, tipo_cliente, sucursal_id,
                     es_organico, cafetalero, numero_mz, comunidad, municipio, departamento, rubros, fecha_foto)
                VALUES (:persona_id, :cedula_hash, :telefono_hashes, TRUE, :estado_asociado, :tipo_cliente, :sucursal_id,
                        :es_organico, :cafetalero, :numero_mz, :comunidad, :municipio, :departamento,
                        CAST(:rubros AS jsonb), CURRENT_DATE)"""), {
                **{k: p[k] for k in ("persona_id", "estado_asociado", "tipo_cliente", "sucursal_id", "es_organico",
                                     "cafetalero", "numero_mz", "comunidad", "municipio", "departamento")},
                # Semillero guarda la cédula como está en ssp.persona ('001-010190-0000A')
                "cedula_hash": hash_determinista(p["cedula"], salt),
                "telefono_hashes": [hash_determinista(p["telefono"], salt)],
                "rubros": json.dumps(p["rubros"], ensure_ascii=False),
            })
            finca = {c: None for c in COLS_FINCA} | p["finca"] | {
                "persona_id": p["persona_id"], "municipio": p["municipio"], "departamento": p["departamento"]}
            await db.execute(text(_insert("perfil_finca", COLS_FINCA)), finca)
            for lote in p["lotes"]:
                await db.execute(text(_insert("perfil_lote", COLS_LOTE)),
                                 {c: None for c in COLS_LOTE} | lote | {"finca_id": p["finca"]["finca_id"]})
        await db.commit()
    return len(PERFILES)


async def quitar() -> None:
    async with AsyncSessionLocal() as db:
        await borrar(db)
        await db.commit()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--borrar", action="store_true", help="quita los socios de prueba")
    args = parser.parse_args()
    if args.borrar:
        asyncio.run(quitar())
        logger.info("Socios de prueba eliminados.")
        return
    n = asyncio.run(sembrar())
    logger.info(f"{n} socios de prueba listos:")
    for p in PERFILES:
        logger.info(f"  cédula {p['cedula']} · WhatsApp {p['telefono'][:4]}-{p['telefono'][4:]} · {p['municipio']} · "
                    f"{', '.join(r['rubro'] for r in p['rubros'])} · pregunta: {p['pregunta']}")


if __name__ == "__main__":
    main()
