#!/usr/bin/env python3
"""
scripts/sync_dag_airflow.py

Script para verificar y sincronizar el DAG 'dag_ingesta.py' entre servidores:
- Origen: VPS4 (servidor actual - /var/www/aldeazon-atv/dags/dag_ingesta.py)
- Destino: VPS2 (servidor de Airflow - flujos.aldea-coffee.com:9959)

Flujo:
1. Conecta vía SSH a VPS2.
2. Identifica la ruta configurada para los DAGs en Airflow (automáticamente o vía configuración).
3. Verifica si 'dag_ingesta.py' ya existe en VPS2.
4. Si NO existe (o si se fuerza actualización), realiza la copia SCP de VPS4 a VPS2.
"""

import os
import sys
import subprocess
import logging
from pathlib import Path
from typing import Optional, Tuple
from dotenv import dotenv_values

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("atv.sync_dag")

# Cargar variables de entorno desde .env si existe
ENV_PATH = Path("/var/www/aldeazon-atv/.env")
env_config = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}

# Configuración con valores por defecto para red interna (VPS2 en 10.0.0.13)
AIRFLOW_HOST = os.getenv("AIRFLOW_HOST", env_config.get("AIRFLOW_HOST", "10.0.0.13"))
AIRFLOW_PORT = os.getenv("AIRFLOW_PORT", env_config.get("AIRFLOW_PORT", "9959"))
AIRFLOW_USER = os.getenv("AIRFLOW_USER", env_config.get("AIRFLOW_USER", "ostendata"))
AIRFLOW_SSH_KEY = os.getenv("AIRFLOW_SSH_KEY", env_config.get("AIRFLOW_SSH_KEY", "~/.ssh/id_cluster"))
AIRFLOW_DAGS_DIR = os.getenv("AIRFLOW_DAGS_DIR", env_config.get("AIRFLOW_DAGS_DIR", ""))

LOCAL_DAG_FILE = Path("/var/www/aldeazon-atv/dags/dag_ingesta.py")

# Rutas estándar en servidores Linux donde suele residir Airflow
CANDIDATE_DAG_DIRS = [
    "/apps/airflow/dags",
    "/opt/airflow/dags",
    "/home/ostendata/airflow/dags",
    "/var/www/airflow/dags",
    "/var/www/aldeazon-atv/dags",
    "~/airflow/dags"
]


def _build_ssh_base_command(key_path: str, port: str, user: str, host: str) -> list:
    expanded_key = os.path.expanduser(key_path)
    cmd = [
        "ssh",
        "-p", str(port),
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=no",
    ]
    if os.path.exists(expanded_key):
        cmd.extend(["-i", expanded_key])
    cmd.append(f"{user}@{host}")
    return cmd


def _build_scp_base_command(key_path: str, port: str) -> list:
    expanded_key = os.path.expanduser(key_path)
    cmd = [
        "scp",
        "-P", str(port),
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=no",
    ]
    if os.path.exists(expanded_key):
        cmd.extend(["-i", expanded_key])
    return cmd


def ejecutar_comando_remoto(cmd_str: str, key_path: str = AIRFLOW_SSH_KEY) -> Tuple[int, str, str]:
    """Ejecuta un comando en VPS2 vía SSH."""
    ssh_cmd = _build_ssh_base_command(key_path, AIRFLOW_PORT, AIRFLOW_USER, AIRFLOW_HOST)
    ssh_cmd.append(cmd_str)

    try:
        proc = subprocess.run(
            ssh_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=25
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "Timeout de conexión SSH con VPS2 (10s excedidos)"
    except Exception as e:
        return -1, "", str(e)


def resolver_directorio_dags_remoto(key_path: str = AIRFLOW_SSH_KEY) -> Optional[str]:
    """
    Determina la ruta de la carpeta de DAGs en VPS2.
    1. Si AIRFLOW_DAGS_DIR está configurado en .env, lo valida en el remoto.
    2. Si no, consulta 'airflow config get-value core dags_folder' en VPS2.
    3. Si no responde, prueba rutas estándar conocidas en VPS2.
    """
    if AIRFLOW_DAGS_DIR:
        code, out, _ = ejecutar_comando_remoto(f"test -d '{AIRFLOW_DAGS_DIR}' && echo 'EXISTS'", key_path)
        if "EXISTS" in out:
            return AIRFLOW_DAGS_DIR
        logger.warning(f"La ruta configurada AIRFLOW_DAGS_DIR='{AIRFLOW_DAGS_DIR}' no existe en VPS2. Buscando automáticamente...")

    # 1. Probar candidatos conocidos en VPS2 (incluye /apps/airflow/dags de Docker)
    for candidate in CANDIDATE_DAG_DIRS:
        code, out, _ = ejecutar_comando_remoto(f"test -d {candidate} && echo 'EXISTS'", key_path)
        if "EXISTS" in out:
            logger.info(f"Ruta de DAGs detectada en VPS2: {candidate}")
            return candidate

    # 2. Intentar preguntar directamente a Airflow CLI
    code, out, _ = ejecutar_comando_remoto("airflow config get-value core dags_folder 2>/dev/null", key_path)
    if code == 0 and out and not out.startswith("Error") and "/" in out:
        logger.info(f"Ruta de DAGs detectada vía 'airflow config': {out}")
        return out

    return None


def verificar_dag_en_airflow(dags_dir: str, dag_filename: str = "dag_ingesta.py", key_path: str = AIRFLOW_SSH_KEY) -> bool:
    """Verifica si el archivo dag_ingesta.py existe en el directorio de DAGs de VPS2."""
    remote_file_path = f"{dags_dir.rstrip('/')}/{dag_filename}"
    code, out, _ = ejecutar_comando_remoto(f"test -f '{remote_file_path}' && echo 'EXISTS'", key_path)
    if "EXISTS" in out:
        return True
    
    # También verificar dentro del contenedor semillero_airflow si está corriendo
    code2, out2, _ = ejecutar_comando_remoto(f"docker exec semillero_airflow test -f '/opt/airflow/dags/{dag_filename}' 2>/dev/null && echo 'EXISTS'", key_path)
    return "EXISTS" in out2


def copiar_dag_a_vps2(dags_dir: str, local_file: Path = LOCAL_DAG_FILE, key_path: str = AIRFLOW_SSH_KEY) -> bool:
    """
    Copia el DAG desde VPS4 hacia el directorio remoto en VPS2 vía SCP.
    Si el destino en el host tiene restricciones de permisos, usa docker cp a través del contenedor.
    """
    if not local_file.exists():
        logger.error(f"El archivo local de DAG no existe: {local_file}")
        return False

    remote_dest = f"{AIRFLOW_USER}@{AIRFLOW_HOST}:{dags_dir.rstrip('/')}/{local_file.name}"
    scp_cmd = _build_scp_base_command(key_path, AIRFLOW_PORT)
    scp_cmd.extend([str(local_file), remote_dest])

    logger.info(f"Copiando {local_file.name} de VPS4 -> VPS2 ({remote_dest})...")
    proc = subprocess.run(
        scp_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30
    )

    if proc.returncode == 0:
        logger.info(f"Copia directa completada con éxito en {remote_dest}")
        return True

    # Si la copia directa falló (por ejemplo, permisos de directorio en el host):
    logger.info("Permiso directo denegado en host. Aplicando transferencia vía contenedor Docker de Airflow...")
    tmp_dest = f"{AIRFLOW_USER}@{AIRFLOW_HOST}:/tmp/{local_file.name}"
    scp_tmp_cmd = _build_scp_base_command(key_path, AIRFLOW_PORT)
    scp_tmp_cmd.extend([str(local_file), tmp_dest])

    proc_tmp = subprocess.run(scp_tmp_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
    if proc_tmp.returncode != 0:
        logger.error(f"Error copiando archivo temporal a /tmp: {proc_tmp.stderr}")
        return False

    # Mover al contenedor mediante docker cp
    move_cmd = f"docker cp /tmp/{local_file.name} semillero_airflow:/opt/airflow/dags/{local_file.name} && rm -f /tmp/{local_file.name}"
    code, out, err = ejecutar_comando_remoto(move_cmd, key_path)
    if code == 0:
        logger.info(f"Copia completada con éxito en semillero_airflow:/opt/airflow/dags/{local_file.name}")
        return True
    else:
        logger.error(f"Error moviendo DAG mediante Docker: {err}")
        return False



def asegurar_dag_en_airflow(force_update: bool = False, key_path: str = AIRFLOW_SSH_KEY) -> bool:
    """
    Función principal:
    1. Comprueba conexión SSH con VPS2.
    2. Resuelve la ruta de DAGs en VPS2.
    3. Verifica si dag_ingesta.py ya existe.
    4. Si no existe (o si force_update=True), lo copia.
    """
    logger.info(f"Comprobando acceso SSH a Airflow en {AIRFLOW_USER}@{AIRFLOW_HOST}:{AIRFLOW_PORT}...")
    code, out, err = ejecutar_comando_remoto("echo 'SSH_OK'", key_path)
    if "SSH_OK" not in out:
        expanded_key = os.path.expanduser(key_path)
        logger.error(f"No fue posible conectar por SSH a VPS2 ({AIRFLOW_HOST}:{AIRFLOW_PORT}).")
        if "Permission denied" in err:
            logger.error(f"Error de autenticación: Llave SSH rechazada o no encontrada en '{expanded_key}'.")
            logger.error("Asegúrate de colocar tu llave privada en esa ruta o especificar AIRFLOW_SSH_KEY=/ruta/llave.")
        else:
            logger.error(f"Detalle del error: {err}")
        return False

    logger.info("Acceso SSH a VPS2 confirmado.")

    # Ubicar directorio de DAGs
    dags_dir = resolver_directorio_dags_remoto(key_path)
    if not dags_dir:
        logger.error("No se pudo detectar el directorio de DAGs en VPS2.")
        logger.error("Define la variable AIRFLOW_DAGS_DIR en .env con la ruta absoluta en VPS2 (ej: /opt/airflow/dags).")
        return False

    # Verificar si dag_ingesta.py ya existe
    dag_name = LOCAL_DAG_FILE.name
    existe = verificar_dag_en_airflow(dags_dir, dag_name, key_path)

    if existe and not force_update:
        logger.info(f"El DAG '{dag_name}' YA existe en Airflow en: {dags_dir}/{dag_name}. No se requiere copia.")
        return True

    if existe and force_update:
        logger.info(f"El DAG '{dag_name}' ya existe, pero se solicitó actualización forzada (--force).")

    if not existe:
        logger.warning(f"El DAG '{dag_name}' NO existe en Airflow ({dags_dir}). Procediendo a copiar...")

    # Copiar archivo
    return copiar_dag_a_vps2(dags_dir, LOCAL_DAG_FILE, key_path)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Verifica y sincroniza dag_ingesta.py hacia el servidor de Airflow (VPS2).")
    parser.add_argument("--force", "-f", action="store_true", help="Fuerza la copia del DAG incluso si ya existe en VPS2.")
    parser.add_argument("--key", "-k", type=str, default=AIRFLOW_SSH_KEY, help="Ruta a la llave privada SSH para conectar a VPS2.")
    parser.add_argument("--dags-dir", "-d", type=str, default="", help="Ruta de DAGs en VPS2 (omite autodetección).")

    args = parser.parse_args()

    if args.dags_dir:
        AIRFLOW_DAGS_DIR = args.dags_dir

    exito = asegurar_dag_en_airflow(force_update=args.force, key_path=args.key)
    sys.exit(0 if exito else 1)
