# dags/dag_perfil_productor.py
import os
import logging
import subprocess

import pendulum

from airflow.sdk import dag, task

logger = logging.getLogger("airflow.task")

# Configuración de conexión hacia VPS4 (Servidor del ATV), igual que dag_ingesta.py
VPS4_HOST = os.getenv("ATV_VPS4_HOST", "10.0.0.15")
VPS4_PORT = os.getenv("ATV_VPS4_PORT", "5775")
VPS4_USER = os.getenv("ATV_VPS4_USER", "ostendata")
VPS4_KEY = os.getenv("ATV_VPS4_KEY", "/opt/airflow/dags/.id_cluster")
PROJECT_DIR = "/var/www/aldeazon-atv"
PYTHON_BIN = f"{PROJECT_DIR}/.venv/bin/python"


def ejecutar_en_vps4(subcomando: str) -> str:
    """
    Ejecuta una tarea del ATV en VPS4 a través de la red interna privada (10.0.0.15:5775).
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
    "retry_delay": pendulum.duration(minutes=15),
}

@dag(
    dag_id="atv_perfil_productor",
    default_args=default_args,
    description="Perfil de socios/clientes y sus fincas (stg_cafe -> atv.perfil_*) e inventario de AldeaZON (stg_insumos -> atv.inventario_producto) para el ATV",
    # Después de las cargas de Semillero que usa: perfil_finca, asociados e
    # insumos_inventario a las 20:00 y cosecha (persona, cliente, comunidad) a
    # las 23:30 de Managua.
    schedule="30 1 * * *",
    start_date=pendulum.datetime(2026, 10, 1, tz="America/Managua"),
    catchup=False,
    max_active_runs=1,
    tags=["atv", "perfil", "inventario", "semillero"],
)
def atv_perfil_productor():
    """
    Reconstruye atv.perfil_productor, atv.perfil_finca y atv.perfil_lote desde la
    última foto de stg_cafe (Semillero, catálogo 004), y atv.inventario_producto
    desde stg_insumos (catálogo 005). Falla sin tocar los perfiles
    vigentes si alguna foto tiene más de 2 días o si el perfil se achica a menos
    de la mitad (ver scripts/construir_perfiles.py).
    """

    @task()
    def construir_perfiles() -> None:
        ejecutar_en_vps4("construir_perfiles")

    @task()
    def construir_inventario() -> None:
        # Independiente del perfil: si falla uno, el otro se reconstruye igual.
        # Ver scripts/construir_inventario.py (mismos gates de frescura y tamaño).
        ejecutar_en_vps4("construir_inventario")

    construir_perfiles()
    construir_inventario()

dag_instance = atv_perfil_productor()
