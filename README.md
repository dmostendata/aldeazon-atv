
# AldeaZON Technical Sales Assistant (ATV) — Core AI Engine

**Iniciativa FA-AZON-ATV** | **OstenData / Familia Aldea (Nicaragua)**

_ODAI Blueprint (Fases 0 — 5)_

## 1\. System Architecture & Executive Summary

The **AldeaZON Technical Sales Assistant (ATV)** is an event-driven, hybrid Retrieval-Augmented Generation (RAG) system designed to deliver grounded agro-technical advisory to sales staff across 15 AldeaZON retail stores in Nicaragua. The system centralizes institutional agronomic knowledge and exposes an internal REST API for integration with a **Client Custom App** deployed at the point of sale.

### End-to-End System Topology

    +---------------------------------------------------------------------------------------------------+
    |                                     ATV SYSTEM ARCHITECTURE                                       |
    |                                                                                                   |
    |   ONLINE QUERY PATH (FastAPI HTTP Server)                                                         |
    |   [Client Custom App] ---> (POST /api/v1/consultar) ---> [PII Sanitizer (Ley 787)]                |
    |                                                                  |                                |
    |                                                                  v                                |
    |   [Redis Cache] <--- [Hybrid RAG (Qdrant)] <--- [Intent Classifier (<=0.3s)]                      |
    |        |                     |                                                                    |
    |        |                     +---> [Confidence & Groundedness Gates]                              |
    |        v                                       |                                                  |
    |   [LLM Wrapper (OpenAI/Gemini)] <--------------+---> (HITL / Abstention Router)                   |
    |        |                                                                                          |
    |        +---> [PostgreSQL 18 Traceability & Audit Log (atv.*)]                                     |
    |                                                                                                   |
    |---------------------------------------------------------------------------------------------------|
    |                                                                                                   |
    |   OFFLINE INGESTION PIPELINE (Apache Airflow DAGs)                                                |
    |   [Document Corpus] ---> [Change Detection] ---> [Extraction Router]                              |
    |   (SeaweedFS S3)                                    |                                             |
    |                                                     +---> (pdfplumber / pypdfium2 / Vision LLM)   |
    |                                                     |                                             |
    |                                                     v                                             |
    |   [Qdrant Alias Switch] <--- [Golden Set QA] <--- [RegistroAtomico DTOs]                          |
    |                                                                                                   |
    +---------------------------------------------------------------------------------------------------+

### Core Technology Stack

| Component | Technology | Engineering Role & Justification |
| --- | --- | --- |
| API Gateway & Serving | Python 3.11+ / FastAPI | Synchronous REST serving layer executing with dependency injection (Depends) and SLA monitoring ($\le 10,000\text{ ms}$). |
| Vector Index (Read Model) | Qdrant 1.9.1 | Hybrid dense (bge-m3, 1024-dim) and sparse lexical retrieval managed via atomic collection alias switches (atv_corpus). |
| Traceability Database | PostgreSQL 18.4 | On-premise audit persistence (atv.consulta, atv.inventario_archivo, atv.error_log) capturing latency micro-metrics and token economics. |
| Document Object Storage | SeaweedFS (S3 API) | Centralized on-premise object storage for agricultural technical sheets (catalogo_objeto / ADR-014). |
| Ingestion Orchestrator | Apache Airflow | Scheduled and event-driven DAG execution (dag_ingesta.py) managing PDF extraction, validation sensors, and vector indexing. |
| Semantic Cache | Redis 7 | High-speed caching layer keyed by cosine similarity ($\ge 0.95$) and active corpus version to bypass LLM generation overhead. |

## 2\. Regulatory Compliance & Governance Safety Gates

*   **Governing Constraint (`RSK-001`):** An incorrect agronomic recommendation (e.g., wrong dosage or incompatible herbicide) can destroy an entire farmer's harvest.
    
*   **Enforced Safety Mechanisms:**
    
    1.  **Groundedness Gate:** The generation wrapper intercepts LLM output and verifies that every numerical dosage figure exists verbatim in the retrieved Qdrant context chunks. Hallucinated figures trigger automatic rejection.
        
    2.  **Confidence & HITL Routing:** Queries falling below similarity confidence thresholds (**0.70** for recommendations, **0.55** for informational queries) bypass LLM generation and route to Human-In-The-Loop (**HITL**) technical assistance.
        
    3.  **Default Abstention:** Queries without matching corpus context emit a mandatory fallback: _"No tengo esa información en las fichas vigentes. Consulta al Área de Asistencia Técnica."_
        
*   **Ley 787 Compliance (Nicaraguan Data Privacy):**
    
    *   All proprietary document corpora, SeaweedFS object stores, Qdrant vector indexes, and PostgreSQL audit tables reside **on-premise** on isolated infrastructure.
        
    *   Prior to dispatching prompts to external LLM APIs (OpenAI / Gemini), a PII Sanitizer (`sanitizar_pii`) scrubs national identification numbers (`[CEDULA]`), phone numbers (`[TELEFONO]`), and proper names via Regex and Named Entity Recognition.
        

## 3\. Internal REST API Specification

The serving layer exposes a synchronous HTTP POST endpoint accessible exclusively within the internal VPC network.

*   **Base URL:** `http://localhost:8100` _(Production VPS4 bind)_
    
*   **Endpoint:** `/api/v1/consultar`
    
*   **HTTP Method:** `POST`
    
*   **Required Headers:**
    
    *   `Content-Type: application/json`
        
    *   `X-API-Key: <ATV_INTERNAL_API_KEY>`
        
*   **SLA Target:** $\\le 10,000\\text{ ms}$ total execution budget ($p95$). Latency budget breaches generate persistent warnings in `atv.error_log`.
    

### Request Payload (`ChatRequest`)

JSON

    {
      "vendedor_id": "usr_987654",
      "consulta": "¿Cuál es la dosis de Consento 45 SC para tomate?",
      "sesion_id": "sess_112233"
    }

### Response Payload (`ChatResponse` — HTTP 200 OK)

JSON

    {
      "respuesta": "Aplica 1,42 - 2,14 l/ha. Valida esta recomendación con Asistencia Técnica antes de aplicarla. [Fuente: Ficha_Consento.pdf, versión 1]",
      "fuente": "Ficha_Consento.pdf, versión 1",
      "escalada_hitl": false,
      "latencia_ms": 3420
    }

## 4\. Local Development Setup (macOS Environment)

Follow these terminal commands to configure and execute your local development baseline on macOS (Apple Silicon / Intel).

### Step 1: System Dependencies (Homebrew)

Bash

    # Update Homebrew package index
    brew update
    
    # Install Python 3.11, PostgreSQL client libraries, and Poppler/PDFium C++ bindings
    brew install python@3.11 postgresql@16 poppler

### Step 2: Virtual Environment & Requirements

Bash

    # Navigate to project root directory
    cd "/path/to/FA-AZON-ATV"
    
    # Initialize isolated Python 3.11 virtual environment
    python3.11 -m venv venv
    
    # Activate virtual environment
    source venv/bin/activate
    
    # Upgrade package manager and install pinned requirements
    pip install --upgrade pip
    pip install -r requirements.txt

### Step 3: Launch Local Topologies (Docker Compose)

Mirrors production VPS3 (`atv_qa` PostgreSQL 18) and VPS4 (`atv_corpus` Qdrant 1.9.1 + Redis 7).

Bash

    # Boot infrastructure containers in background
    docker compose up -d
    
    # Verify container health checks
    docker compose ps

### Step 4: Environment Variables (`.env`)

Create an `.env` configuration file in the project root:

Ini, TOML

    # Persistence (Local Docker Compose mapping host port 5434 -> container port 5432)
    DATABASE_URL="postgresql+psycopg://aldeazon_user:secret@localhost:5434/atv_qa"
    
    # API Gateway Security
    API_KEY="aldeazon_internal_app_secret_2026"
    
    # Qdrant Vector DB & Storage
    QDRANT_URL="http://localhost:6333"
    QDRANT_API_KEY=""
    QDRANT_COLLECTION="atv_corpus"
    
    # External LLM Providers
    OPENAI_API_KEY="sk-proj-your-openai-api-key"

### Step 5: Execute Local Development Server

Bash

    # Start FastAPI application server with hot-reload enabled
    uvicorn app.main:app --host 127.0.0.1 --port 8100 --reload

## 5\. Offline Ingestion Pipeline (Airflow & Extraction Router)

The offline ingestion engine transforms unstructured technical PDFs stored in SeaweedFS into atomic, embeddable vectors (`RegistroAtomico`).

1.  **Change Detection (`scripts/ingest_sharepoint.py`):** Compares document ETags against `atv.inventario_archivo` to process only modified or newly uploaded PDFs.
    
2.  **Extraction Router (`app/services/extractor.py`):**
    
    *   **Deterministic Path:** Primary parsing via `pdfplumber` (Structured Tables $\\rightarrow$ Aligned Columns $\\rightarrow$ Prose).
        
    *   **Memory Healing Fallback:** Recovers broken PDF/XREF headers using C++ `pypdfium2`.
        
    *   **Vision Path (§6.5.4):** Scanned static images are routed to multimodal LLMs for literal Markdown transcription without summarization.
        
3.  **Atomic Indexing & Alias Switching:**
    
    *   Extracts validated agronomic tuples (`producto`, `cultivo`, `dosis`, `condiciones`).
        
    *   Generates 1024-dimensional dense vectors (`intfloat/multilingual-e5-large` / `bge-m3`) and sparse lexical vectors.
        
    *   Upserts records into a newly versioned Qdrant collection (`atv_corpus_v{N}`).
        
    *   Executes an **Atomic Alias Switch** (`atv_corpus` $\\rightarrow$ `atv_corpus_v{N}`) and purges the semantic Redis cache to guarantee zero downtime.
        

## 6\. Verification & End-to-End Testing

Verify local API gateway functionality using `curl`:

Bash

    curl -X POST "http://127.0.0.1:8100/api/v1/consultar" \
         -H "accept: application/json" \
         -H "Content-Type: application/json" \
         -H "X-API-Key: aldeazon_internal_app_secret_2026" \
         -d '{
               "vendedor_id": "vendedor_qa_01",
               "consulta": "¿Qué dosis de Consento 45 SC recomiendo para broca en café?",
               "sesion_id": "test_macbook_01"
             }'

## 7\. Codebase Directory Structure

Plaintext

    .
    ├── app/
    │   ├── core/
    │   │   ├── config.py             # Pydantic BaseSettings singleton (Environment & Secrets)
    │   │   └── database.py           # SQLAlchemy sync engine & SessionLocal factory
    │   ├── db/
    │   │   └── session.py            # Async SQLAlchemy engine & connection pool management
    │   ├── models/
    │   │   ├── telemetry.py          # SQLAlchemy ORM definitions (atv.consulta, atv.error_log)
    │   │   └── traceability.py       # SQLAlchemy ORM definitions (atv.inventario_archivo)
    │   ├── schemas/
    │   │   └── registro_atomico.py   # Pydantic domain models & embedding text serializer
    │   ├── services/
    │   │   ├── extractor.py          # PDF Extraction Router (pdfplumber -> pypdfium2 -> Vision)
    │   │   ├── logger_service.py     # Non-blocking async APM & SLA audit logging
    │   │   └── rag_service.py        # Hybrid RAG, PII Sanitizer, Groundedness Gate & HITL routing
    │   └── main.py                   # FastAPI REST API Gateway (/api/v1/consultar)
    ├── dags/
    │   └── dag_ingesta.py            # DAG orquestador en Airflow (VPS2) con delegación SSH a VPS4
    ├── scripts/
    │   ├── airflow_tasks.py          # Entrypoint CLI de ejecución remota en VPS4 para tareas de Airflow
    │   ├── bootstrap_qdrant.py       # Idempotent collection bootstrap and alias attachment
    │   ├── inspect_qdrant.py         # Diagnostic CLI tool for collections, count, and payloads
    │   ├── sync_dag_airflow.py       # Verificación y copia de dag_ingesta.py de VPS4 a VPS2
    │   ├── ingest_seaweedfs.py       # Pipeline de ingesta offline y sync SeaweedFS S3 -> Qdrant
    │   └── ingest_sharepoint.py      # Offline synchronization & Qdrant collection builder
    ├── fixtures/                     # Local sample PDFs and test data
    ├── docker-compose.yml            # Local dev topology (Postgres 16, Qdrant 1.9.1, Redis 7)
    ├── requirements.txt              # Pinned Python ecosystem dependencies
    └── README.md                     # System documentation & architectural baseline

## 8\. Qdrant Vector Database Setup & Replication Guide

Para replicar o desplegar la configuración del vector store en una nueva instancia de Qdrant:

### 8.1. Especificación Técnica de la Colección

* **Colección base:** `atv_corpus_v1`
* **Alias activo:** `atv_corpus` (referenciado en `.env` mediante `QDRANT_COLLECTION`).
* **Modelo de Embeddings:** `intfloat/multilingual-e5-large` (manejado vía FastEmbed).
* **Tipo de vector:** Vector denso único (**Single Dense Vector**). *No utiliza vectores dispersos (sparse vectors).*
* **Nombre del vector:** `fast-multilingual-e5-large` *(nomenclatura requerida por FastEmbed).*
* **Dimensiones:** `1024`
* **Métrica de distancia:** `Cosine`
* **Almacenamiento de payload:** `on_disk_payload: true`
* **Parámetros HNSW:** `m: 16`, `ef_construct: 100`

---

### 8.2. Creación vía Dashboard Web de Qdrant

En la interfaz gráfica (`http://<HOST>:6333/dashboard#/collections`):

1. **What'\''s your use case?**: Seleccionar `Global search`.
2. **What to use for search?**: Seleccionar `Simple Single embedding` (o `Custom`), **no** seleccionar `Simple Hybrid Search`.
3. **Collection Name**: `atv_corpus_v1`
4. **Vector name**: `fast-multilingual-e5-large`
5. **Dimensions**: `1024`
6. **Choose metric**: `Cosine`
7. Crear la colección y posteriormente asignar el alias `atv_corpus`.

---

### 8.3. Creación automatizada vía API REST (`curl`)

```bash
# 1. Crear colección base
curl -X PUT "http://<HOST>:6333/collections/atv_corpus_v1" \
  -H "Content-Type: application/json" \
  -H "api-key: <TU_QDRANT_API_KEY>" \
  -d '{
    "vectors": {
      "fast-multilingual-e5-large": {
        "size": 1024,
        "distance": "Cosine"
      }
    },
    "on_disk_payload": true,
    "hnsw_config": {
      "m": 16,
      "ef_construct": 100
    }
  }'

# 2. Asociar el Alias atv_corpus
curl -X POST "http://<HOST>:6333/collections/aliases" \
  -H "Content-Type: application/json" \
  -H "api-key: <TU_QDRANT_API_KEY>" \
  -d '{
    "actions": [
      {
        "create_alias": {
          "collection_name": "atv_corpus_v1",
          "alias_name": "atv_corpus"
        }
      }
    ]
  }'
```

---

### 8.4. Inicialización y Diagnóstico con Scripts Python

El proyecto incluye utilidades CLI listas para validar y poblar el nuevo servidor:

```bash
# Inicializar la colección y asociar alias de forma idempotente
python3 -m scripts.bootstrap_qdrant

# Inspeccionar el estado de la colección, conteo de vectores y muestras de payload
python3 -m scripts.inspect_qdrant
```

---

### 8.5. Variables de Entorno (`.env`)

Asegurar que el archivo `.env` apunte al nuevo host:

```ini
QDRANT_URL="http://<HOST_O_IP>:6333"
QDRANT_API_KEY="<TU_API_KEY>"
QDRANT_COLLECTION="atv_corpus"
```

## 9\. Airflow Orchestrator & Multi-VPS DAG Synchronization (VPS4 ↔ VPS2)

El pipeline de ingesta offline está orquestado por **Apache Airflow**, el cual se ejecuta en un servidor dedicado (**VPS2**), mientras que el código base de la aplicación y extracción reside en **VPS4**.

### 9.1. Topología de Red Privada (`10.0.0.0/24`)

Toda la comunicación entre servidores se realiza a través de la red local privada de alta velocidad (latencia $< 1\text{ ms}$), sin exponer tráfico a internet:

| Servidor | Rol en la Arquitectura | IP Interna | Servicios & Puertos |
| :--- | :--- | :--- | :--- |
| **VPS4** | API Gateway, RAG Service, Cómputo IA & Extracción (Servidor Local) | `10.0.0.15` | OpenSSH (`5775`), FastAPI (`8100`), Código ATV (`/var/www/aldeazon-atv`), venv Python 3.12 |
| **VPS2** | Orquestador Airflow (DAG Scheduler, API & Workers) | `10.0.0.13` | OpenSSH (`9959`), Airflow UI (`8080`), Docker (`semillero_airflow`, Python 3.10) |
| **VPS3** | Capa de Persistencia & Datalake | `10.0.0.14` | PostgreSQL (`5432`), Qdrant Vector DB (`6333`), SeaweedFS S3 (`8333`) |

---

### 9.2. Emparejamiento Seguro SSH Bidireccional entre VPS4 y VPS2

Para garantizar que la orquestación y la transferencia de archivos operen de forma desatendida y sin contraseñas interactivas:

1. **De VPS4 hacia VPS2 (Sincronización de DAGs):**
   * VPS4 cuenta con una clave SSH de cluster en `~/.ssh/id_cluster` (`ed25519`).
   * La clave pública está autorizada en `~/.ssh/authorized_keys` del usuario `ostendata` en `10.0.0.13:9959`.
   * Permite ejecutar SCP y comandos de Docker en VPS2 desde scripts de despliegue.

2. **De VPS2 (Contenedor Airflow) hacia VPS4 (Despacho de Tareas de Ingesta):**
   * Dentro del contenedor `semillero_airflow`, la clave privada reside en `/opt/airflow/dags/.id_cluster` (`chmod 600`).
   * La clave pública está registrada en `~/.ssh/authorized_keys` de VPS4 (`10.0.0.15:5775`).
   * Permite a las tareas de Airflow despachar comandos de ejecución hacia VPS4 mediante SSH BatchMode sin intervención manual.

3. **Verificación de Enlace Bidireccional:**
   ```bash
   # 1. Desde VPS4 hacia VPS2
   ssh -p 9959 -i ~/.ssh/id_cluster ostendata@10.0.0.13 "echo 'VPS4_TO_VPS2_OK'"

   # 2. Desde el contenedor de Airflow en VPS2 hacia VPS4
   ssh -p 9959 -i ~/.ssh/id_cluster ostendata@10.0.0.13 \
     "docker exec semillero_airflow ssh -p 5775 -i /opt/airflow/dags/.id_cluster -o StrictHostKeyChecking=no ostendata@10.0.0.15 'echo VPS2_TO_VPS4_OK'"
   ```

---

### 9.3. Despliegue de DAGs en Airflow (VPS2)

En VPS2, Airflow opera en un contenedor Docker (`semillero_airflow`). El directorio de DAGs está mapeado como bind mount:
* **Host VPS2:** `/apps/airflow/dags/`
* **Contenedor Airflow:** `/opt/airflow/dags/`

Cualquier archivo `.py` depositado en `/apps/airflow/dags/` es indexado automáticamente por el planificador de Airflow.

---

### 9.4. Script de Sincronización Automática (`sync_dag_airflow.py`)

El script [`scripts/sync_dag_airflow.py`](file:///var/www/aldeazon-atv/scripts/sync_dag_airflow.py) gestiona el ciclo de vida del DAG entre servidores:

1. Valida conectividad SSH hacia VPS2 por la red interna (`10.0.0.13:9959`).
2. Comprueba si [`dags/dag_ingesta.py`](file:///var/www/aldeazon-atv/dags/dag_ingesta.py) ya existe en `/apps/airflow/dags/`.
3. Si **no existe** (o si se fuerza la actualización con `--force`), realiza la transferencia segura vía SCP y Docker hacia el directorio de DAGs de Airflow.
4. Si **ya existe**, omite la transferencia para garantizar idempotencia.

#### Comandos de Ejecución CLI:

```bash
# Verificación e instalación inicial (idempotente)
python3 -m scripts.sync_dag_airflow

# Forzar sobrescritura del DAG en VPS2 ante modificaciones en el código
python3 -m scripts.sync_dag_airflow --force

# Parámetros manuales opcionales
python3 -m scripts.sync_dag_airflow --key ~/.ssh/id_cluster --dags-dir /apps/airflow/dags
```

---

### 9.5. Integración en el Pipeline de Ingesta

El proceso principal de ingesta offline ([`scripts/ingest_seaweedfs.py`](file:///var/www/aldeazon-atv/scripts/ingest_seaweedfs.py)) invoca automáticamente esta verificación al inicio de su flujo:

```bash
python3 -m scripts.ingest_seaweedfs
```

Antes de escanear o descargar archivos de SeaweedFS, el script valida que Airflow en VPS2 tenga el DAG disponible para orquestar las tareas subsecuentes.

---

### 9.6. Variables de Entorno (`.env`)

Configuración correspondiente en el archivo [`.env`](file:///var/www/aldeazon-atv/.env):

```ini
# AIRFLOW SERVER (VPS2 - Red Interna)
AIRFLOW_HOST="10.0.0.13"
AIRFLOW_PORT="9959"
AIRFLOW_USER="ostendata"
AIRFLOW_SSH_KEY="~/.ssh/id_cluster"
AIRFLOW_DAGS_DIR="/apps/airflow/dags"
```

---

### 9.7. Arquitectura Desacoplada: Orquestación (VPS2) vs. Cómputo RAG (VPS4)

#### Motivación del Desacoplamiento:
El contenedor de Airflow en VPS2 (`semillero_airflow`) corre Python 3.10 y alberga servicios compartidos del cluster (OpenMetadata 1.13.1, providers de AWS y Amazon). Instalar directamente en el contenedor las librerías de IA y RAG de ATV (`fastembed`, `qdrant-client`, `pdfplumber`, `pypdfium2`, `spacy`, `torch`/`onnxruntime`) provocaba conflictos destructivos de dependencias (degradación de `boto3`/`botocore`).

#### Principio de Solución:
* **Airflow (VPS2):** Actúa exclusivamente como **Orquestador Central**. Mantiene el grafo de dependencias, la planificación horaria, el sensor HITL y el control de reintentos.
* **ATV Engine (VPS4):** Actúa como el **Nodo de Cómputo Especializado**. Ejecuta las fases pesadas dentro de su entorno virtual nativo (`/var/www/aldeazon-atv/.venv`), donde residen todas las dependencias y conexiones directas a Qdrant y PostgreSQL.
* **Mecanismo de Despacho:** Cada tarea del DAG [`dags/dag_ingesta.py`](file:///var/www/aldeazon-atv/dags/dag_ingesta.py) invoca internamente la función `ejecutar_en_vps4(subcomando)`, la cual ejecuta vía SSH por la red privada:
  ```bash
  ssh -p 5775 -i /opt/airflow/dags/.id_cluster ostendata@10.0.0.15 \
    "cd /var/www/aldeazon-atv && .venv/bin/python -m scripts.airflow_tasks <subcomando>"
  ```

---

### 9.8. Módulo de Tareas en VPS4 (`scripts/airflow_tasks.py`)

Punto de entrada unificado que expone cada fase del pipeline como un subcomando CLI ejecutable:

| Subcomando | Función Ejecutada | Descripción Operativa |
| :--- | :--- | :--- |
| `sincronizar_s3` | `task_sincronizar_s3()` | Conecta a SeaweedFS S3 (`10.0.0.14:8333`), analiza ETags frente a `atv.inventario_archivo`, detecta deltas y descarga los PDFs a `fixtures/staging/`. |
| `obtener_pendientes` | `task_obtener_pendientes()` | Consulta PostgreSQL e imprime un arreglo JSON con los nombres de archivos en estado `NUEVO` o `MODIFICADO`. |
| `extraer_pdf <archivo>` | `task_extraer_pdf(filename)` | Ejecuta la extracción dual (determinista vía `pdfplumber` o visión LLM vía `pypdfium2` + GPT-4o-mini), registrando los registros atómicos. |
| `validar_hitl` | `task_validar_hitl()` | Consulta PostgreSQL para validar si existen registros pendientes de validación humana (`estado IN ('en_revision', 'EN_REVISION')`). Emite `True` o `False`. |
| `construir_y_promover` | `task_construir_y_promover()` | Genera embeddings densos (`intfloat/multilingual-e5-large`), crea la colección versionada en Qdrant, realiza el cambio atómico de alias (`atv_corpus`) y purga la caché Redis. |

---

### 9.9. Flujo y Ciclo de Vida del DAG (`dags/dag_ingesta.py`)

El DAG `atv_corpus_ingestion_pipeline` se estructura en el siguiente grafo secuencial:

```text
[t1_sincronizar_s3]
        │
        ▼
[t2_identificar_archivos_pendientes]
        │
        ├── (Si no hay archivos pendientes) ──> [AirflowSkipException] ──> [DAG Run: SUCCESS (Skipped)]
        │
        └── (Si hay archivos pendientes)
                │
                ▼
        [t3_extraer_pdf]  (Dynamic Task Mapping: expande 1 tarea por PDF)
                │
                ▼
        [t4_sensor_validacion_hitl]  (Sensor: verifica cada 1h resolución humana)
                │
                ▼
        [t5_construir_indice_y_promover]  (Qdrant Index & Alias Switch)
```

* **Idempotencia y Skip Elegante:** Cuando no se detectan fichas técnicas nuevas o modificadas en S3, la tarea `t2` emite `AirflowSkipException("Sin archivos pendientes para procesar.")`. Airflow salta las fases de extracción, sensor y promoción, concluyendo la ejecución en estado **`success`** sin generar falsas alarmas ni bloqueos.

---

### 9.10. Comandos Operativos de Validación y Monitoreo CLI

Para monitorear o disparar el pipeline directamente desde la terminal de VPS4:

```bash
# 1. Sincronizar / actualizar el DAG hacia Airflow (VPS2)
python3 -m scripts.sync_dag_airflow --force

# 2. Disparar manualmente una ejecución del pipeline
ssh -p 9959 -i ~/.ssh/id_cluster ostendata@10.0.0.13 \
  "docker exec semillero_airflow airflow dags trigger atv_corpus_ingestion_pipeline"

# 3. Listar el historial y estado de ejecuciones del DAG
ssh -p 9959 -i ~/.ssh/id_cluster ostendata@10.0.0.13 \
  "docker exec semillero_airflow airflow dags list-runs atv_corpus_ingestion_pipeline"

# 4. Inspeccionar el estado individual de cada tarea de una ejecución específica
ssh -p 9959 -i ~/.ssh/id_cluster ostendata@10.0.0.13 \
  "docker exec semillero_airflow airflow tasks states-for-dag-run atv_corpus_ingestion_pipeline <RUN_ID>"

# 5. Probar una tarea individual en modo de prueba (Dry Run)
ssh -p 9959 -i ~/.ssh/id_cluster ostendata@10.0.0.13 \
  "docker exec semillero_airflow airflow tasks test atv_corpus_ingestion_pipeline t1_sincronizar_s3"
```