# dags/dag_ingesta.py
import os
import json
import pendulum
import logging
import subprocess
from typing import List

from airflow.sdk import dag, task
from airflow.sdk.exceptions import AirflowSkipException

logger = logging.getLogger("airflow.task")

# Configuración de conexión hacia VPS4 (Servidor de Ingesta & Embeddings)
VPS4_HOST = os.getenv("ATV_VPS4_HOST", "10.0.0.15")
VPS4_PORT = os.getenv("ATV_VPS4_PORT", "5775")
VPS4_USER = os.getenv("ATV_VPS4_USER", "ostendata")
VPS4_KEY = os.getenv("ATV_VPS4_KEY", "/opt/airflow/dags/.id_cluster")
PROJECT_DIR = "/var/www/aldeazon-atv"
PYTHON_BIN = f"{PROJECT_DIR}/.venv/bin/python"


def ejecutar_en_vps4(subcomando: str) -> str:
    """
    Ejecuta una tarea del pipeline en VPS4 a través de la red interna privada (10.0.0.15:5775).
    """
    cmd_remoto = f"cd {PROJECT_DIR} && {PYTHON_BIN} -m scripts.airflow_tasks {subcomando}"
    ssh_cmd = [
        "ssh",
        "-p", str(VPS4_PORT),
        "-i", VPS4_KEY,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        f"{VPS4_USER}@{VPS4_HOST}",
        cmd_remoto
    ]
    logger.info(f"Despachando tarea hacia VPS4 ({VPS4_HOST}:{VPS4_PORT}): {subcomando}")
    proc = subprocess.run(ssh_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        logger.error(f"Fallo en ejecución remota en VPS4 (código {proc.returncode}): {proc.stderr}")
        raise RuntimeError(f"Fallo en VPS4: {proc.stderr.strip()}")
    
    if proc.stdout.strip():
        logger.info(f"Salida de VPS4:\n{proc.stdout.strip()}")
    return proc.stdout.strip()


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
    Orquesta y delega la ejecución de las fases en VPS4 por la red interna.
    """

    @task()
    def t1_sincronizar_s3() -> None:
        """
        Phase 1: SeaweedFS S3 ETag Synchronization.
        Detects deltas without downloading binary payloads.
        """
        logger.info("Iniciando fase 1: Detección de deltas en SeaweedFS S3...")
        ejecutar_en_vps4("sincronizar_s3")

    @task()
    def t2_identificar_archivos_pendientes() -> List[str]:
        """
        Queries PostgreSQL to identify files marked as 'NUEVO' or 'MODIFICADO'.
        If no files require extraction, raises AirflowSkipException to halt the DAG gracefully.
        """
        raw_output = ejecutar_en_vps4("obtener_pendientes")
        lineas = [l.strip() for l in raw_output.splitlines() if l.strip()]
        json_line = lineas[-1] if lineas else "[]"
        try:
            pendientes = json.loads(json_line)
        except Exception:
            pendientes = []

        if not pendientes:
            logger.info("No se encontraron fichas técnicas modificadas o nuevas. Finalizando pipeline.")
            raise AirflowSkipException("Sin archivos pendientes para procesar.")

        logger.info(f"Archivos identificados para extracción: {pendientes}")
        return pendientes

    @task()
    def t3_extraer_pdf(filename: str) -> str:
        """
        Phase 2: Extraction Router.
        Uses Dynamic Task Mapping to run parallel extraction (Deterministic/Vision) per file.
        """
        logger.info(f"Extrayendo registros de: {filename}")
        ejecutar_en_vps4(f"extraer_pdf '{filename}'")
        return filename

    @task.sensor(poke_interval=3600, timeout=86400, mode="reschedule")
    def t4_sensor_validacion_hitl() -> bool:
        """
        Phase 3: HITL Validation Gate (RSK-001).
        Suspends the Airflow worker and checks PostgreSQL every hour.
        Advances only when ALL records have been manually approved/rejected by Sergio Espinosa.
        """
        logger.info("Verificando estado de revisiones HITL en PostgreSQL...")
        raw_output = ejecutar_en_vps4("validar_hitl")
        lineas = [l.strip() for l in raw_output.splitlines() if l.strip()]
        is_ready = lineas[-1] == "True" if lineas else False
        if not is_ready:
            logger.info("Existen registros pendientes de aprobación en HITL. Suspendiendo ejecución...")
        return is_ready

    @task()
    def t5_construir_indice_y_promover() -> None:
        """
        Phase 4: Golden Set Regression & Zero-Downtime Deployment.
        Embeds approved chunks, evaluates precision, and swaps Qdrant alias.
        """
        logger.info("Construyendo índices vectoriales y promoviendo alias en Qdrant...")
        ejecutar_en_vps4("construir_y_promover")

    # =====================================================================
    # DAG GRAPH DEFINITION (Dependencies)
    # =====================================================================
    sync_task = t1_sincronizar_s3()
    archivos_pendientes = t2_identificar_archivos_pendientes()
    extraccion_tasks = t3_extraer_pdf.expand(filename=archivos_pendientes)
    sensor_hitl = t4_sensor_validacion_hitl()
    deploy_task = t5_construir_indice_y_promover()

    sync_task >> archivos_pendientes >> extraccion_tasks >> sensor_hitl >> deploy_task

dag_instance = atv_ingestion_pipeline()