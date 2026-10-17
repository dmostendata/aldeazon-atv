"""
Reconstruye el corpus de fichas en una colección NUEVA de Qdrant con la
extracción por ficha (app/services/extractor_ficha.py), la compara con la
vigente y, cuando se valida, la promueve. Mientras no se promueve, la API sigue
leyendo la colección vigente: construir y probar no tocan el alias.

    python -m scripts.reindexar_corpus construir --version 2 [--workers 4] [--solo "FT X.pdf" ...] [--forzar]
    python -m scripts.reindexar_corpus probar    --version 2 "¿qué tiene para roya?" ...
    python -m scripts.reindexar_corpus promover  --version 2

construir
  Todos los PDF del inventario (atv.inventario_archivo, salvo RETIRADO) que
  están en fixtures/staging. Por archivo borra sus puntos en la colección y
  sube los nuevos, así que se puede cortar y volver a correr: retoma desde el
  reporte (fixtures/reindex_v<N>.json) y salta los ya hechos salvo --forzar.
  Las llamadas al LLM van en paralelo (--workers); los embeddings, en este
  proceso (multilingual-e5-large, ~2 GB de RAM).

probar
  Para cada consulta muestra las fichas que devuelve la colección vigente y
  la nueva (producto, cultivo, dosis, score). Solo lectura.

promover
  Exige que el reporte cubra todo el inventario. Actualiza el inventario
  (estado, registros, método, versión), mueve el alias a la colección nueva y
  vacía la caché de Redis (ingest_seaweedfs.promover_indice_y_purgar_cache).
  La colección anterior NO se borra: volver atrás = promover --version 1.
"""

import argparse
import asyncio
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, List

from qdrant_client import QdrantClient
from sqlalchemy import select, update

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.traceability import InventarioArchivo
from app.services.extractor_ficha import extraer_ficha
from scripts.ingest_seaweedfs import (
    ALIAS_NAME, EMBEDDING_MODEL, STAGING_DIR, _ensure_qdrant_collection,
    promover_indice_y_purgar_cache, reemplazar_registros_documento, version_vigente,
)

logger = logging.getLogger("atv.reindexar_corpus")

DIR_REPORTES = os.path.join(os.path.dirname(STAGING_DIR))


def ruta_reporte(version: int) -> str:
    return os.path.join(DIR_REPORTES, f"reindex_v{version}.json")


def leer_reporte(version: int) -> Dict[str, dict]:
    try:
        with open(ruta_reporte(version), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def guardar_reporte(version: int, reporte: Dict[str, dict]) -> None:
    tmp = ruta_reporte(version) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reporte, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, ruta_reporte(version))


async def archivos_inventario() -> Dict[str, str]:
    """basename -> nombre_archivo (ruta S3) de todo lo no retirado."""
    async with AsyncSessionLocal() as s:
        filas = (await s.execute(
            select(InventarioArchivo.nombre_archivo).where(InventarioArchivo.estado != "RETIRADO")
        )).scalars().all()
    return {os.path.basename(n): n for n in sorted(filas)}


def construir(version: int, workers: int, solo: List[str], forzar: bool) -> None:
    coleccion = f"{ALIAS_NAME}_v{version}"
    qdrant = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    if version == version_vigente(qdrant):
        raise SystemExit(f"'{coleccion}' es la colección vigente (alias '{ALIAS_NAME}'): no se reconstruye en caliente.")
    # threads=2: cada hilo de ONNX reserva su propia memoria; con los 4 por defecto el
    # proceso llegó a ~2,7 GB y dejó a la API sin RAM (2026-10-17).
    qdrant.set_model(EMBEDDING_MODEL, threads=2)
    _ensure_qdrant_collection(qdrant, coleccion)

    inventario = asyncio.run(archivos_inventario())
    reporte = leer_reporte(version)
    pendientes = [f for f in inventario if (not solo or f in solo)
                  and (forzar or f not in reporte or reporte[f].get("error"))]
    faltan_en_disco = [f for f in pendientes if not os.path.exists(os.path.join(STAGING_DIR, f))]
    for f in faltan_en_disco:
        reporte[f] = {"nombre_archivo": inventario[f], "registros": 0, "error": "No está en fixtures/staging"}
    pendientes = [f for f in pendientes if f not in faltan_en_disco]
    logger.info(f"{len(inventario)} archivos en inventario, {len(pendientes)} por procesar en '{coleccion}'.")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futuros = {pool.submit(extraer_ficha, os.path.join(STAGING_DIR, f), f, version): f for f in pendientes}
        for i, futuro in enumerate(as_completed(futuros), 1):
            f = futuros[futuro]
            r = futuro.result()
            if not r.error:
                try:
                    reemplazar_registros_documento(qdrant, coleccion, f, r.registros)
                except Exception as exc:
                    r.error = f"Qdrant: {type(exc).__name__}: {str(exc)[:200]}"
            reporte[f] = {
                "nombre_archivo": inventario[f], "metodo": r.metodo, "caracteres_texto": r.caracteres_texto,
                "usos_llm": r.usos_llm, "registros": 0 if r.error else len(r.registros),
                "con_dosis_area": sum(1 for x in r.registros if x.dosis) if not r.error else 0,
                "dosis_descartadas": r.dosis_descartadas, "error": r.error,
                "productos": sorted({x.producto for x in r.registros}),
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            guardar_reporte(version, reporte)
            logger.info(f"[{i}/{len(pendientes)}] {f}: {reporte[f]['registros']} registros"
                        + (f" — ERROR {r.error}" if r.error else ""))

    ok = [v for v in reporte.values() if not v.get("error")]
    logger.info(f"Listo: {len(ok)}/{len(reporte)} archivos con registros, "
                f"{sum(v['registros'] for v in ok)} registros en '{coleccion}'. Reporte: {ruta_reporte(version)}")


def probar(version: int, consultas: List[str], top_k: int) -> None:
    qdrant = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    qdrant.set_model(EMBEDDING_MODEL)
    vigente = version_vigente(qdrant)
    for consulta in consultas:
        print(f"\n### {consulta}")
        for v in dict.fromkeys((vigente, version)):
            print(f"  -- {ALIAS_NAME}_v{v}{' (vigente)' if v == vigente else ''}")
            for h in qdrant.query(f"{ALIAS_NAME}_v{v}", query_text=consulta, limit=top_k):
                m = h.metadata
                print(f"     {h.score:.3f}  {m.get('producto')} | {str(m.get('cultivo'))[:40]} | "
                      f"{str(m.get('objetivo'))[:35]} | dosis={m.get('dosis')}")


async def promover(version: int) -> None:
    reporte = leer_reporte(version)
    inventario = await archivos_inventario()
    faltan = sorted(set(inventario) - set(reporte))
    if faltan:
        raise SystemExit(f"El reporte v{version} no cubre {len(faltan)} archivos (p. ej. {faltan[:3]}): corra 'construir'.")
    ok = sum(1 for v in reporte.values() if not v.get("error"))
    if ok < len(inventario) / 2:
        raise SystemExit(f"Solo {ok}/{len(inventario)} archivos con registros: no se promueve.")

    ahora = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as s:
        for f, r in reporte.items():
            if f not in inventario:
                continue
            await s.execute(
                update(InventarioArchivo).where(InventarioArchivo.nombre_archivo == inventario[f]).values(
                    estado="INDEXADO" if r["registros"] > 0 and not r.get("error") else "ERROR",
                    total_registros_atomicos=r["registros"],
                    metodo_extraccion=r.get("metodo") or "prosa",
                    version_corpus=version,
                    ts_indexado=ahora,
                )
            )
        await s.commit()
    await promover_indice_y_purgar_cache(version_nueva=version)
    logger.info(f"Alias '{ALIAS_NAME}' -> '{ALIAS_NAME}_v{version}'. Inventario actualizado ({ok} indexados).")


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("construir"); p.add_argument("--version", type=int, required=True)
    p.add_argument("--workers", type=int, default=2); p.add_argument("--solo", nargs="*", default=[])
    p.add_argument("--forzar", action="store_true")
    p = sub.add_parser("probar"); p.add_argument("--version", type=int, required=True)
    p.add_argument("--top-k", type=int, default=3); p.add_argument("consultas", nargs="+")
    p = sub.add_parser("promover"); p.add_argument("--version", type=int, required=True)
    args = parser.parse_args()

    if args.cmd == "construir":
        construir(args.version, args.workers, args.solo, args.forzar)
    elif args.cmd == "probar":
        probar(args.version, args.consultas, args.top_k)
    else:
        asyncio.run(promover(args.version))


if __name__ == "__main__":
    main()
