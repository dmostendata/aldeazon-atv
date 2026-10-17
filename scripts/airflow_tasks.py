# scripts/airflow_tasks.py
"""
Puntos de entrada CLI para las tareas de Airflow que se ejecutan en VPS4.
Invocados remotamente por los DAGs 'dag_ingesta.py' y 'dag_perfil_productor.py' desde VPS2.
"""

import sys
import os
import json
import asyncio
import logging

from sqlalchemy import select, func
from app.db.session import AsyncSessionLocal
from app.models.traceability import InventarioArchivo
from scripts.ingest_seaweedfs import (
    sincronizar_seaweedfs,
    reconstruir_indice_qdrant,
    promover_indice_y_purgar_cache,
    CURRENT_CORPUS_VERSION,
    STAGING_DIR,
)
from app.services.extractor import extraer_desde_pdf

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("atv.airflow_tasks")


async def task_sincronizar_s3():
    logger.info("Iniciando sincronización con SeaweedFS S3...")
    await sincronizar_seaweedfs()
    logger.info("Sincronización SeaweedFS completada.")


async def task_obtener_pendientes():
    async with AsyncSessionLocal() as db:
        query = select(InventarioArchivo.nombre_archivo).where(
            InventarioArchivo.estado.in_(["NUEVO", "MODIFICADO"])
        )
        res = await db.execute(query)
        pendientes = list(res.scalars().all())
    # Imprime exclusivamente JSON en la última línea para que Airflow lo capture
    print(json.dumps(pendientes))


def task_extraer_pdf(filename: str):
    base_name = os.path.basename(filename)
    filepath = os.path.join(STAGING_DIR, base_name)
    if not os.path.exists(filepath):
        filepath = os.path.join(STAGING_DIR, filename)
    if not os.path.exists(filepath):
        logger.warning(f"Archivo '{filename}' no encontrado en staging ({filepath}).")
        return

    registros = list(extraer_desde_pdf(filepath, base_name))
    logger.info(f"Extracción exitosa: {len(registros)} registros atómicos de '{base_name}'.")


async def task_validar_hitl():
    async with AsyncSessionLocal() as db:
        query = select(func.count(InventarioArchivo.archivo_id)).where(
            InventarioArchivo.estado.in_(["en_revision", "EN_REVISION"])
        )
        count = (await db.execute(query)).scalar()
        is_ready = (count == 0)
    print(str(is_ready))


async def task_construir_y_promover():
    logger.info("Reconstruyendo índice en Qdrant...")
    await asyncio.to_thread(reconstruir_indice_qdrant)
    logger.info(f"Promoviendo alias a versión {CURRENT_CORPUS_VERSION} y purgando caché...")
    await promover_indice_y_purgar_cache(version_nueva=CURRENT_CORPUS_VERSION)
    logger.info("Despliegue y promoción completados con éxito.")


def main():
    if len(sys.argv) < 2:
        print("Uso: python -m scripts.airflow_tasks [sincronizar_s3|obtener_pendientes|extraer_pdf|validar_hitl|construir_y_promover|construir_perfiles|reiniciar_cargas]")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "sincronizar_s3":
        asyncio.run(task_sincronizar_s3())
    elif cmd == "obtener_pendientes":
        asyncio.run(task_obtener_pendientes())
    elif cmd == "extraer_pdf":
        if len(sys.argv) < 3:
            logger.error("Debe especificar el nombre del archivo para extraer_pdf.")
            sys.exit(1)
        task_extraer_pdf(sys.argv[2])
    elif cmd == "validar_hitl":
        asyncio.run(task_validar_hitl())
    elif cmd == "construir_y_promover":
        asyncio.run(task_construir_y_promover())
    elif cmd == "construir_perfiles":
        from scripts.construir_perfiles import construir_perfiles
        print(json.dumps(asyncio.run(construir_perfiles())))
    elif cmd == "reiniciar_cargas":
        from scripts.reset_inventory import ejecutar_reinicio_completo
        ejecutar_reinicio_completo(confirm=True, delete_db=True)
    else:
        logger.error(f"Comando desconocido: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
