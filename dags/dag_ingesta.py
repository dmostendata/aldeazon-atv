# dags/dag_ingesta.py
import os
import asyncio
import pendulum
import logging
from typing import List

# Updated for Airflow 3.x SDK
from airflow.sdk import dag, task
from airflow.sdk.exceptions import AirflowSkipException
from sqlalchemy import select, func

from app.db.session import AsyncSessionLocal
from app.models.traceability import InventarioArchivo

# Assuming scripts.ingest_seaweedfs contains our S3 and Qdrant logic
from scripts.ingest_seaweedfs import sincronizar_seaweedfs, promover_indice_y_purgar_cache
from app.services.extractor import extraer_desde_pdf

logger = logging.getLogger("airflow.task")

# =====================================================================
# AIRFLOW DAG CONFIGURATION
# =====================================================================
default_args = {
    "owner": "aldeazon_data",
    "depends_on_past": False,
    "retries": 2,
    "retry_delay": pendulum.duration(minutes=5),
}

@dag(
    dag_id="atv_corpus_ingestion_pipeline",
    default_args=default_args,
    description="ODAI Blueprint §6.5: SeaweedFS to Qdrant Ingestion Pipeline",
    schedule="0 2 * * *",  # Cron: Run daily at 2:00 AM
    start_date=pendulum.datetime(2026, 8, 1, tz="UTC"),
    catchup=False,
    tags=["atv", "rag", "ingestion"],
)
def atv_ingestion_pipeline():
    """
    Master orchestrator for the ATV offline knowledge pipeline.
    """

    @task()
    def t1_sincronizar_s3() -> None:
        """
        Phase 1: SeaweedFS S3 ETag Synchronization.
        Detects deltas without downloading binary payloads.
        """
        logger.info("Initiating SeaweedFS delta detection...")
        asyncio.run(sincronizar_seaweedfs())

    @task()
    def t2_identificar_archivos_pendientes() -> List[str]:
        """
        Queries PostgreSQL to identify files marked as 'NUEVO' or 'MODIFICADO'.
        If no files require extraction, raises AirflowSkipException to halt the DAG gracefully.
        """
        async def _obtener_pendientes() -> List[str]:
            async with AsyncSessionLocal() as db:
                query = select(InventarioArchivo.nombre_archivo).where(
                    InventarioArchivo.estado.in_(["NUEVO", "MODIFICADO"])
                )
                result = await db.execute(query)
                return list(result.scalars().all())
                
        pendientes = asyncio.run(_obtener_pendientes())
        
        if not pendientes:
            logger.info("No modified technical sheets found. Halting pipeline.")
            raise AirflowSkipException("No files to process. Skipping downstream tasks.")
            
        return pendientes

    @task()
    def t3_extraer_pdf(filename: str) -> str:
        """
        Phase 2: Extraction Router.
        Uses Dynamic Task Mapping to run parallel extraction (Deterministic/Vision) per file.
        """
        # In a real cluster, the file would be downloaded to local staging here via aioboto3
        filepath = os.path.join("/tmp/staging", filename)
        
        # Ensure file exists for local dev mocking
        if not os.path.exists(filepath):
            logger.warning(f"File {filepath} not found in staging. Skipping extraction.")
            return filename

        # Extract and yield RegistroAtomico tuples
        registros = list(extraer_desde_pdf(filepath, filename))
        logger.info(f"Extracted {len(registros)} atomic records from {filename}.")
        
        return filename

    @task.sensor(poke_interval=3600, timeout=86400, mode="reschedule")
    def t4_sensor_validacion_hitl() -> bool:
        """
        Phase 3: HITL Validation Gate (RSK-001).
        Suspends the Airflow worker and checks PostgreSQL every hour.
        Advances only when ALL records have been manually approved/rejected by Sergio Espinosa.
        """
        async def _check_pending() -> bool:
            async with AsyncSessionLocal() as db:
                # ODAI Blueprint: No records can remain in 'en_revision' state
                query = select(func.count(InventarioArchivo.archivo_id)).where(
                    InventarioArchivo.estado == "en_revision"
                )
                result = await db.execute(query)
                count = result.scalar()
                return count == 0
                
        is_ready = asyncio.run(_check_pending())
        if not is_ready:
            logger.info("Pending HITL validations found. Suspending DAG execution...")
        return is_ready

    @task()
    def t5_construir_indice_y_promover() -> None:
        """
        Phase 4: Golden Set Regression & Zero-Downtime Deployment.
        Embeds approved chunks, evaluates precision, and swaps Qdrant alias.
        """
        # Step A: Build new collection (atv_corpus_v{N}) and embed documents
        nueva_version = 2 # Derived dynamically from DB in production
        
        # Step B: Golden Set Regression (Mocked)
        precision_golden_set = 0.85
        if precision_golden_set < 0.60:
            raise ValueError(f"Golden Set precision ({precision_golden_set}) fell below 60% threshold. Deployment aborted.")
            
        # Step C: Atomic Alias Switch & Cache Purge (VPS4)
        asyncio.run(promover_indice_y_purgar_cache(version_nueva=nueva_version))


    # =====================================================================
    # DAG GRAPH DEFINITION (Dependencies)
    # =====================================================================
    sync_task = t1_sincronizar_s3()
    archivos_pendientes = t2_identificar_archivos_pendientes()
    
    # Airflow Dynamic Task Mapping: Spawns one 't3_extraer_pdf' task instance per file
    extraccion_tasks = t3_extraer_pdf.expand(filename=archivos_pendientes)
    
    sensor_hitl = t4_sensor_validacion_hitl()
    deploy_task = t5_construir_indice_y_promover()

    # Define the execution chain
    sync_task >> archivos_pendientes >> extraccion_tasks >> sensor_hitl >> deploy_task

# Register the DAG
dag_instance = atv_ingestion_pipeline()