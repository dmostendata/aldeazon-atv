# scripts/reset_inventory.py
"""
Herramienta de reinicio y purga del pipeline de ingesta ATV (PostgreSQL + Qdrant + Redis).

Permite eliminar o reiniciar las cargas anteriores registradas en la base de datos de
trazabilidad PostgreSQL (atv.inventario_archivo y atv.error_log), limpiar colecciones
en Qdrant y purgar la cache semantica de Redis, para iniciar una ingesta limpia desde cero.

Por defecto ejecuta una simulacion (DRY RUN). Requiere el flag --confirm para aplicar los cambios.

Uso:
    # 1. Simulacion (muestra que se borraria sin alterar nada):
    python -m scripts.reset_inventory

    # 2. Reinicio completo: Borra registros de postgres, colecciones Qdrant y cache Redis:
    python -m scripts.reset_inventory --confirm

    # 3. Soft reset: En vez de borrar las filas de postgres, resetea su estado a 'NUEVO':
    python -m scripts.reset_inventory --confirm --reset-status

    # 4. Reinicio completo eliminando tambien los PDFs descargados en fixtures/staging:
    python -m scripts.reset_inventory --confirm --clean-staging
"""
import os
import sys
import argparse
import logging
from typing import Tuple, List

from sqlalchemy import create_engine, update, func, select, delete
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from qdrant_client import QdrantClient
import redis

from app.core.config import settings
from app.models.traceability import InventarioArchivo, ErrorLog

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("atv.reset_inventory")

ALIAS_NAME = settings.qdrant_collection
CURRENT_CORPUS_VERSION = 1
COLLECTION_NAME = f"{ALIAS_NAME}_v{CURRENT_CORPUS_VERSION}"
STAGING_DIR = "/var/www/aldeazon-atv/fixtures/staging"

engine = create_engine(settings.database_url, poolclass=NullPool)
Session = sessionmaker(bind=engine)


def borrar_carga_anterior_db(confirm: bool) -> Tuple[int, int]:
    """
    Elimina fisicamente los registros de la carga anterior en PostgreSQL:
    - atv.inventario_archivo (todos los archivos rastreados)
    - atv.error_log (errores asociados a la ingesta/Airflow)
    """
    db = Session()
    try:
        count_inv = db.execute(select(func.count(InventarioArchivo.archivo_id))).scalar() or 0
        count_err = db.execute(
            select(func.count(ErrorLog.error_id)).where(
                (ErrorLog.componente.ilike("%ingest%")) | (ErrorLog.componente.ilike("%airflow%"))
            )
        ).scalar() or 0

        if not confirm:
            logger.info(
                f"[DRY RUN] PostgreSQL: Se eliminarian {count_inv} registro(s) de 'atv.inventario_archivo' "
                f"y {count_err} error(es) de ingesta en 'atv.error_log'."
            )
            return count_inv, count_err

        # Borrado en atv.inventario_archivo
        stmt_inv = delete(InventarioArchivo)
        res_inv = db.execute(stmt_inv)

        # Borrado de errores asociados a ingesta en atv.error_log
        stmt_err = delete(ErrorLog).where(
            (ErrorLog.componente.ilike("%ingest%")) | (ErrorLog.componente.ilike("%airflow%"))
        )
        res_err = db.execute(stmt_err)

        db.commit()
        logger.warning(
            f"[POSTGRESQL] Eliminados {res_inv.rowcount} registro(s) de 'atv.inventario_archivo' "
            f"y {res_err.rowcount} registro(s) de ingesta en 'atv.error_log'."
        )
        return res_inv.rowcount, res_err.rowcount
    finally:
        db.close()


def reset_inventario_estado_db(confirm: bool, target_state: str = "NUEVO") -> int:
    """
    Soft-reset: Mantiene las filas existentes en atv.inventario_archivo pero
    cambia su estado a target_state ('NUEVO'), reiniciando contadores y timestamps.
    """
    db = Session()
    try:
        count = db.execute(select(func.count(InventarioArchivo.archivo_id))).scalar() or 0

        if not confirm:
            logger.info(
                f"[DRY RUN] PostgreSQL: Se actualizarian {count} archivo(s) en 'atv.inventario_archivo' "
                f"al estado '{target_state}' (total_registros_atomicos=0, ts_indexado=None)."
            )
            return count

        stmt = (
            update(InventarioArchivo)
            .values(
                estado=target_state,
                total_registros_atomicos=0,
                ts_indexado=None,
            )
        )
        result = db.execute(stmt)
        db.commit()
        logger.info(
            f"[POSTGRESQL] {result.rowcount} archivo(s) en 'atv.inventario_archivo' actualizados a '{target_state}'. "
            f"Listos para ser reprocesados en la siguiente ejecucion."
        )
        return result.rowcount
    finally:
        db.close()


def wipe_qdrant_collections(confirm: bool) -> List[str]:
    """
    Elimina en Qdrant cualquier coleccion que corresponda al corpus:
    - Coleccion con nombre exacto del alias (e.g., 'atv_corpus')
    - Colecciones versionadas (e.g., 'atv_corpus_v1', 'atv_corpus_v2', etc.)
    """
    qdrant = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)

    try:
        collections_resp = qdrant.get_collections()
        all_cols = [c.name for c in collections_resp.collections]
    except Exception as exc:
        logger.error(f"[QDRANT] Error consultando colecciones: {exc}")
        return []

    # Identificar colecciones del corpus a eliminar
    target_cols = [
        name for name in all_cols
        if name == ALIAS_NAME or name.startswith(f"{ALIAS_NAME}_v")
    ]

    if not target_cols:
        logger.info(f"[QDRANT] No se encontraron colecciones con prefijo '{ALIAS_NAME}'. Nada que borrar.")
        return []

    deleted = []
    for col in target_cols:
        try:
            pt_count = qdrant.count(collection_name=col).count
        except Exception:
            pt_count = 0

        if not confirm:
            logger.info(f"[DRY RUN] Qdrant: Se eliminaria la coleccion '{col}' ({pt_count} puntos vectoriales).")
        else:
            try:
                qdrant.delete_collection(col)
                logger.warning(f"[QDRANT] Coleccion '{col}' eliminada ({pt_count} puntos vectoriales purgados).")
                deleted.append(col)
            except Exception as exc:
                logger.error(f"[QDRANT] Error eliminando coleccion '{col}': {exc}")

    return deleted


def flush_redis_cache(confirm: bool) -> bool:
    """
    Purga la base de datos de Redis para invalidar cache semantica previa.
    """
    if not confirm:
        logger.info(f"[DRY RUN] Redis: Se ejecutaria flushdb en {settings.redis_url}.")
        return False

    try:
        r = redis.from_url(settings.redis_url)
        r.flushdb()
        logger.info("[REDIS] Cache semantica purgada exitosamente.")
        return True
    except Exception as exc:
        logger.warning(f"[REDIS] No se pudo purgar la cache de Redis: {exc}")
        return False


def clean_staging_files(confirm: bool) -> int:
    """
    Elimina los archivos PDF almacenados en el directorio local de staging.
    """
    if not os.path.exists(STAGING_DIR):
        return 0

    archivos = [f for f in os.listdir(STAGING_DIR) if f.lower().endswith(".pdf")]

    if not confirm:
        logger.info(f"[DRY RUN] Staging: Se eliminarian {len(archivos)} archivo(s) PDF en '{STAGING_DIR}'.")
        return len(archivos)

    eliminados = 0
    for f in archivos:
        path = os.path.join(STAGING_DIR, f)
        try:
            os.remove(path)
            eliminados += 1
        except Exception as exc:
            logger.error(f"[STAGING] Error eliminando archivo '{f}': {exc}")

    logger.info(f"[STAGING] Eliminados {eliminados} archivo(s) PDF de '{STAGING_DIR}'.")
    return eliminados


def ejecutar_reinicio_completo(
    confirm: bool = True,
    delete_db: bool = True,
    reset_status: bool = False,
    wipe_qdrant_flag: bool = True,
    flush_redis_flag: bool = True,
    clean_staging_flag: bool = False,
) -> dict:
    """
    Punto de entrada programatico para reiniciar completamente las cargas.
    """
    resultado = {}

    if delete_db and not reset_status:
        inv_del, err_del = borrar_carga_anterior_db(confirm)
        resultado["inventario_borrados"] = inv_del
        resultado["errores_borrados"] = err_del
    else:
        actualizados = reset_inventario_estado_db(confirm, target_state="NUEVO")
        resultado["inventario_actualizados"] = actualizados

    if wipe_qdrant_flag:
        resultado["qdrant_colecciones_borradas"] = wipe_qdrant_collections(confirm)

    if flush_redis_flag:
        resultado["redis_purgado"] = flush_redis_cache(confirm)

    if clean_staging_flag:
        resultado["staging_archivos_borrados"] = clean_staging_files(confirm)

    return resultado


def main():
    parser = argparse.ArgumentParser(
        description="Reiniciar todas las cargas: Limpia PostgreSQL (atv.inventario_archivo), Qdrant y Redis."
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Aplica los cambios destructivos. Si no se incluye, ejecuta una simulacion (Dry Run)."
    )
    parser.add_argument(
        "--delete",
        "--delete-db",
        dest="delete_db",
        action="store_true",
        default=True,
        help="Borra fisicamente los registros de la carga anterior en atv.inventario_archivo y errores en atv.error_log (por defecto)."
    )
    parser.add_argument(
        "--reset-status",
        action="store_true",
        help="En lugar de borrar las filas de postgres, actualiza su estado a 'NUEVO' para que sean reprocesadas."
    )
    parser.add_argument(
        "--keep-qdrant",
        action="store_true",
        help="No borra colecciones existentes en Qdrant."
    )
    parser.add_argument(
        "--keep-redis",
        action="store_true",
        help="No purga la cache de Redis."
    )
    parser.add_argument(
        "--clean-staging",
        action="store_true",
        help="Elimina los PDFs locales en fixtures/staging para forzar la re-descarga desde SeaweedFS S3."
    )

    args = parser.parse_args()

    # Si se especifico --reset-status, no se borran las filas fisicamente
    delete_db_mode = args.delete_db and not args.reset_status

    if not args.confirm:
        logger.info("=" * 65)
        logger.info("MODO SIMULACION (DRY RUN) - Ningun dato sera modificado.")
        logger.info("Ejecute con --confirm para aplicar los cambios.")
        logger.info("=" * 65)

    ejecutar_reinicio_completo(
        confirm=args.confirm,
        delete_db=delete_db_mode,
        reset_status=args.reset_status,
        wipe_qdrant_flag=not args.keep_qdrant,
        flush_redis_flag=not args.keep_redis,
        clean_staging_flag=args.clean_staging,
    )

    if not args.confirm:
        logger.info("=" * 65)
        logger.info("Para ejecutar el reinicio definitivo:")
        logger.info("  python -m scripts.reset_inventory --confirm")
        logger.info("=" * 65)


if __name__ == "__main__":
    main()