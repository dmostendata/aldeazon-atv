
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
        
    3.  **Default Abstention:** Queries without matching corpus context emit a mandatory fallback: _"No tengo esa información en las fichas vigentes. Consulte al Área de Asistencia Técnica."_ Only this phrase escalates; the mandatory closing line of every recommendation (_"Valide esta recomendación con Asistencia Técnica antes de aplicarla."_) does not.

    4.  **Scope Gate:** A separate classification call (`alcance_prompt`) answers only agricultural and Aldea Global topics; anything else gets `msg_fuera_alcance` without reaching retrieval (see §11.5).
        
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
    

The agent first profiles the user over several turns (see §11): the web client sends `sesion_id` empty on the first message and then echoes back the `sesion_id` it receives.

### Request Payload (`ChatRequest`)

JSON

    {
      "consulta": "¿Qué producto me recomienda para la roya del café?",
      "sesion_id": "3f9c2a7e5b1d4c0e8a6f2b9d7e1c4a50",
      "sucursal_id": null,
      "vendedor_id": "web"
    }

### Response Payload (`ChatResponse` — HTTP 200 OK)

Profiling turn:

    {
      "respuesta": "¿En qué municipio o departamento está su finca (o su negocio)?",
      "etapa": "perfilamiento",
      "sesion_id": "3f9c2a7e5b1d4c0e8a6f2b9d7e1c4a50",
      "perfil": {"tipo_usuario": "productor", "cultivos": [{"nombre": "café", "area_mz": 2.0}], "...": "..."},
      "fuente": null,
      "escalada_hitl": false,
      "carrito": [],
      "latencia_ms": 1640
    }

Answer turn:

    {
      "respuesta": "Para el control de la roya del café, le recomiendo considerar MISTRAL EXTRA 32 SC, con una dosis de 0.4 a 0.6 L/ha... Valide esta recomendación con Asistencia Técnica antes de aplicarla. [Fuente: FICHA TÉCNICA MISTRAL EXTRA 32 SC.pdf, versión 1]",
      "etapa": "respuesta",
      "sesion_id": "3f9c2a7e5b1d4c0e8a6f2b9d7e1c4a50",
      "perfil": {"...": "..."},
      "fuente": "FICHA TÉCNICA MISTRAL EXTRA 32 SC.pdf, versión 1",
      "escalada_hitl": false,
      "carrito": [],
      "latencia_ms": 2566
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
    │   │   ├── perfil_cliente.py     # Perfil del usuario, hash compatible con Semillero, lectura de atv.perfil_*
    │   │   ├── perfilamiento.py      # Conversación de perfilamiento por sesión (preguntas, extracción LLM)
    │   │   ├── prompt_config.py      # Gestor de configuración y validación de prompts desde PostgreSQL
    │   │   └── rag_service.py        # Scope gate, Hybrid RAG, Groundedness Gate & HITL routing
    │   └── main.py                   # FastAPI REST API Gateway (/api/v1/consultar)
    ├── dags/
    │   ├── dag_ingesta.py            # DAG orquestador en Airflow (VPS2) con delegación SSH a VPS4
    │   └── dag_perfil_productor.py   # DAG diario (01:30) que reconstruye atv.perfil_* desde stg_cafe
    ├── docs/
    │   └── instrucciones_admin_vps2_perfil_atv.pdf  # pg_hba.conf y GRANT para el admin de PostgreSQL
    ├── scripts/
    │   ├── 001_create_atv_schema.sql # Esquema base DDL (PostgreSQL)
    │   ├── 002_create_prompt_config.sql # DDL y semillas para atv.prompt_config
    │   ├── 003_create_perfil.sql     # atv.perfil_*, atv.sesion_perfil y claves de perfilamiento
    │   ├── 004_prompt_vendedor.sql   # Rol de vendedor, trato de usted, filtro de alcance
    │   ├── construir_perfiles.py     # stg_cafe (base semillero) -> atv.perfil_* (base staging)
    │   ├── semillero/004_cafe_perfil_finca.sql # Registro de las tablas de finca en el catálogo de Semillero
    │   ├── airflow_tasks.py          # Entrypoint CLI de ejecución remota en VPS4 para tareas de Airflow
    │   ├── bootstrap_qdrant.py       # Idempotent collection bootstrap and alias attachment
    │   ├── inspect_qdrant.py         # Diagnostic CLI tool for collections, count, and payloads
    │   ├── sync_dag_airflow.py       # Copia un DAG de VPS4 a VPS2 (--archivo, por defecto dag_ingesta.py)
    │   ├── ingest_seaweedfs.py       # Pipeline de ingesta offline y sync SeaweedFS S3 -> Qdrant (soporta --reset)
    │   ├── ingest_sharepoint.py      # Offline synchronization & Qdrant collection builder
    │   ├── reset_inventory.py        # Herramienta de reinicio completo: purga PostgreSQL, Qdrant y Redis
    │   └── reiniciar_cargas.py       # Alias CLI interactivo y directo para reiniciar cargas de ingesta
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

### 9.5. Modos de Ejecución: Pipeline Directo Local (VPS4) vs. DAG Calendarizado (Airflow VPS2)

El sistema de ingesta y generación de embeddings soporta **dos modalidades operativas complementarias** que comparten el mismo núcleo algorítmico:

| Criterio | Modo 1: Reproceso Manual Directo (`ingest_seaweedfs.py`) | Modo 2: Orquestación Calendarizada (`dag_ingesta.py`) |
| :--- | :--- | :--- |
| **Punto de Ejecución** | Servidor local **VPS4** (`.venv/bin/python`) | **Apache Airflow** en **VPS2** despachando vía SSH a VPS4 |
| **Propósito Principal** | Reconstrucciones completas inmediatas, setup inicial, depuración o tras vaciado total de la base de datos. | Ingesta desatendida diaria, detección incremental de deltas y gobierno con supervisión humana. |
| **Planificación Temporal** | Ejecución inmediata bajo demanda por consola | Calendarizado diario a las **2:00 AM UTC** (`0 2 * * *`) |
| **Paralelismo** | Secuencial por archivo con sub-lotes por PDF | *Dynamic Task Mapping* (Airflow genera 1 tarea concurrente por cada PDF) |
| **Puerta de Control HITL** | Omitida (construye y promueve directamente a Qdrant) | Incluye sensor horario (`t4_sensor_validacion_hitl`) para aprobación previa |
| **Detección de Cambios** | Procesa PDFs nuevos/modificados y salta los ya en `INDEXADO` | Emite `AirflowSkipException` si no hay cambios, finalizando con éxito en $< 5\text{ s}$ |

---

### 9.6. Guía Paso a Paso: Reprocesos Manuales Directos en VPS4

Utilice este flujo cuando necesite reconstruir el corpus vectorial de forma inmediata sin esperar la ventana horaria ni el sensor de Airflow:

#### Paso 1: Diagnosticar el Estado Actual
Verifique el contenido del inventario en PostgreSQL y las colecciones en Qdrant:
```bash
# Inspeccionar estado de colecciones vectoriales en Qdrant
python -m scripts.inspect_qdrant
```

#### Paso 2: (Opcional) Purgar Cargas Anteriores
Si la base de datos vectorial quedó desincronizada o se actualizaron los algoritmos de extracción y se requiere empezar 100% desde cero:
```bash
# Simular qué se eliminaría (Dry Run)
python -m scripts.reiniciar_cargas

# Ejecutar el borrado definitivo en PostgreSQL, Qdrant y Redis
python -m scripts.reiniciar_cargas --confirm
```

#### Paso 3: Ejecutar la Ingesta Directa
Ejecute el proceso principal en VPS4. Existen dos alternativas:

* **Opción A (Recomendada tras purga):** Ejecutar la ingesta estándar:
  ```bash
  python -m scripts.ingest_seaweedfs
  ```
* **Opción B (Todo en uno):** Purgar la carga previa e iniciar la ingesta en un único comando:
  ```bash
  python -m scripts.ingest_seaweedfs --reset
  ```

#### Paso 4: Monitorear el Proceso en Segundo Plano
La extracción y vectorización de decenas de PDFs técnicos puede tardar varios minutos:
```bash
# Verificar que el proceso sigue activo y qué recursos consume
ps aux | grep ingest_seaweedfs

# Consultar el avance en PostgreSQL (en otra terminal)
python -c "
from app.core.config import settings
from sqlalchemy import create_engine, text
with create_engine(settings.database_url).connect() as conn:
    print(conn.execute(text('SELECT estado, count(*) FROM atv.inventario_archivo GROUP BY estado')).fetchall())
"
```

#### Paso 5: Validar la Colección y el Agente RAG
Una vez completado el proceso:
```bash
# 1. Comprobar que Qdrant tiene los puntos vectoriales y el alias activo
python -m scripts.inspect_qdrant

# 2. Realizar una consulta de prueba al API
curl -i -X POST "http://127.0.0.1:8100/api/v1/consultar" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: aldeazon_internal_app_secret_2026" \
  -d '{
    "vendedor_id": "usr_test_01",
    "sucursal_id": 1,
    "consulta": "¿Cuál es la dosis foliar recomendada de Engorlid para café?",
    "sesion_id": "sess_01"
  }'
```

---

### 9.7. Variables de Entorno (`.env`)

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

### 9.8. Arquitectura Desacoplada: Orquestación (VPS2) vs. Cómputo RAG (VPS4)

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

### 9.9. Módulo de Tareas en VPS4 (`scripts/airflow_tasks.py`)

Punto de entrada unificado que expone cada fase del pipeline como un subcomando CLI ejecutable:

| Subcomando | Función Ejecutada | Descripción Operativa |
| :--- | :--- | :--- |
| `sincronizar_s3` | `task_sincronizar_s3()` | Conecta a SeaweedFS S3 (`10.0.0.14:8333`), analiza ETags frente a `atv.inventario_archivo`, detecta deltas y descarga los PDFs a `fixtures/staging/`. |
| `obtener_pendientes` | `task_obtener_pendientes()` | Consulta PostgreSQL e imprime un arreglo JSON con los nombres de archivos en estado `NUEVO` o `MODIFICADO`. |
| `extraer_pdf <archivo>` | `task_extraer_pdf(filename)` | Ejecuta la extracción dual (determinista vía `pdfplumber` o visión LLM vía `pypdfium2` + GPT-4o-mini), registrando los registros atómicos. |
| `validar_hitl` | `task_validar_hitl()` | Consulta PostgreSQL para validar si existen registros pendientes de validación humana (`estado IN ('en_revision', 'EN_REVISION')`). Emite `True` o `False`. |
| `construir_y_promover` | `task_construir_y_promover()` | Genera embeddings densos (`intfloat/multilingual-e5-large`), crea la colección versionada en Qdrant, realiza el cambio atómico de alias (`atv_corpus`) y purga la caché Redis. |
| `reiniciar_cargas` | `ejecutar_reinicio_completo()` | Purga física de `atv.inventario_archivo`, errores de ingesta en `atv.error_log`, eliminación de colecciones en Qdrant y purga de caché Redis. |

---

### 9.10. Configuración y Despliegue del DAG en Airflow Paso a Paso (`dags/dag_ingesta.py`)

Siga esta guía paso a paso para configurar, sincronizar y operar el DAG en Apache Airflow:

#### Paso 1: Configurar Conectividad SSH y Credenciales
Asegúrese de que el par de claves esté habilitado en ambos extremos:
1. En VPS4, confirme que existe la clave del cluster: `~/.ssh/id_cluster`.
2. En VPS2, asegúrese de que el contenedor de Airflow tenga montada la clave en `/opt/airflow/dags/.id_cluster` con permisos `600`.
3. Verifique el enlace bidireccional:
   ```bash
   # Comprobar VPS4 -> VPS2
   ssh -p 9959 -i ~/.ssh/id_cluster ostendata@10.0.0.13 "echo 'CONEXION_OK'"

   # Comprobar contenedor Airflow en VPS2 -> VPS4
   ssh -p 9959 -i ~/.ssh/id_cluster ostendata@10.0.0.13 \
     "docker exec semillero_airflow ssh -p 5775 -i /opt/airflow/dags/.id_cluster -o StrictHostKeyChecking=no ostendata@10.0.0.15 'echo CONEXION_OK'"
   ```

#### Paso 2: Sincronizar el DAG hacia Airflow (VPS2)
Cada vez que se modifique [`dags/dag_ingesta.py`](file:///var/www/aldeazon-atv/dags/dag_ingesta.py), despliéguelo hacia el directorio de DAGs de Airflow:
```bash
python -m scripts.sync_dag_airflow --force
```
Este script copia el archivo a `/apps/airflow/dags/` en VPS2 (montado en `/opt/airflow/dags/` dentro del contenedor).

#### Paso 3: Validar que el DAG fue Indexado en Airflow
Verifique en el planificador de Airflow que el DAG está reconocido y no tiene errores de sintaxis (*import errors*):
```bash
ssh -p 9959 -i ~/.ssh/id_cluster ostendata@10.0.0.13 \
  "docker exec semillero_airflow airflow dags list | grep atv_corpus"
```
Debe listar: `atv_corpus_ingestion_pipeline`.

#### Paso 4: Comprender el Ciclo de Vida y Grafo de Tareas
El DAG `atv_corpus_ingestion_pipeline` ejecuta el siguiente flujo secuencial:

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
        [t3_extraer_pdf]  (Dynamic Task Mapping: 1 tarea paralela por PDF)
                │
                ▼
        [t4_sensor_validacion_hitl]  (Sensor horario: espera aprobación humana)
                │
                ▼
        [t5_construir_indice_y_promover]  (Vectorización, Alias Switch & Purga Redis)
```

* **Comportamiento Idempotente:** Si SeaweedFS no contiene archivos modificados, `t2` emite `AirflowSkipException`. Airflow finaliza el ciclo en estado **`success` (Skipped)** en pocos segundos, ahorrando costos de LLM y procesamiento.
* **Control de Calidad (HITL):** Si alguna ficha requiere verificación agronómica manual, la tarea `t4` entra en suspensión (`mode="reschedule"`) y reintenta cada hora sin consumir recursos de worker.

#### Paso 5: Comandos Operativos de Monitoreo y Disparo Manual en Airflow
Comandos para controlar el pipeline directamente desde la consola de VPS4:

```bash
# 1. Disparar manualmente una ejecución del DAG fuera de horario
ssh -p 9959 -i ~/.ssh/id_cluster ostendata@10.0.0.13 \
  "docker exec semillero_airflow airflow dags trigger atv_corpus_ingestion_pipeline"

# 2. Listar las últimas ejecuciones del DAG y su estado (success, running, failed)
ssh -p 9959 -i ~/.ssh/id_cluster ostendata@10.0.0.13 \
  "docker exec semillero_airflow airflow dags list-runs atv_corpus_ingestion_pipeline"

# 3. Ver el estado detallado de cada tarea dentro de una ejecución específica
ssh -p 9959 -i ~/.ssh/id_cluster ostendata@10.0.0.13 \
  "docker exec semillero_airflow airflow tasks states-for-dag-run atv_corpus_ingestion_pipeline <RUN_ID>"

# 4. Probar una tarea individual en modo de prueba (Dry Run)
ssh -p 9959 -i ~/.ssh/id_cluster ostendata@10.0.0.13 \
  "docker exec semillero_airflow airflow tasks test atv_corpus_ingestion_pipeline t1_sincronizar_s3"
```

---

### 9.11. Reinicio y Purga Completa del Pipeline de Ingesta (`scripts/reiniciar_cargas.py` / `scripts/reset_inventory.py`)

Cuando se requiere reconstruir el corpus vectorial desde cero (por ejemplo, si la colección de Qdrant quedó vacía pero PostgreSQL registraba una carga previa, tras corregir algoritmos de extracción o para resolver discrepancias en el inventario), se utiliza el módulo de reinicio.

#### Acciones Ejecutadas por el Reinicio:
1. **PostgreSQL (`atv.inventario_archivo`):** Elimina físicamente todos los registros de fichas técnicas previas (o restablece su estado a `NUEVO` si se utiliza `--reset-status`).
2. **PostgreSQL (`atv.error_log`):** Purga registros de errores previos asociados al proceso de ingesta y tareas de Airflow (`componente LIKE '%ingest%'` o `%airflow%`).
3. **Qdrant Vector DB:** Elimina de forma segura cualquier colección y alias previo (`atv_corpus`, `atv_corpus_v1`, etc.), evitando colisiones al reasignar el alias.
4. **Redis Cache:** Ejecuta `flushdb` para purgar la caché semántica previa y asegurar que las nuevas respuestas provengan del corpus actualizado.
5. **Fixtures / Staging (Opcional):** Con `--clean-staging`, elimina los PDFs locales en `fixtures/staging` para forzar la re-descarga completa desde SeaweedFS S3.

#### Comandos Disponibles:

```bash
# 1. Simulación (Dry Run): muestra qué registros, colecciones y caches se purgarían sin alterar nada
python -m scripts.reiniciar_cargas

# 2. Reinicio y purga definitiva (PostgreSQL + Qdrant + Redis):
python -m scripts.reiniciar_cargas --confirm

# 3. Soft-Reset: Mantiene los registros en PostgreSQL pero restablece su estado a 'NUEVO' y limpia Qdrant:
python -m scripts.reiniciar_cargas --confirm --reset-status

# 4. Reinicio completo eliminando también los PDFs locales en staging:
python -m scripts.reiniciar_cargas --confirm --clean-staging

# 5. Reinicio + Ingesta completa en un solo comando:
python -m scripts.ingest_seaweedfs --reset

# 6. Ejecución delegada a través de las tareas de Airflow:
python -m scripts.airflow_tasks reiniciar_cargas
```

---

## 10\. Configuración Dinámica de Prompts en PostgreSQL (`atv.prompt_config`)

Toda la configuración del system prompt, plantillas de interacción con el LLM, mensajes de error y parámetros de generación de `rag_service.py` se gestiona en la tabla `atv.prompt_config` de PostgreSQL.

* **Fuente única de verdad:** No existen valores hardcodeados en el código como fallback. Si la base de datos no está disponible o falta una clave requerida, la API responde de forma controlada con **HTTP 503** y registra el evento en `atv.error_log`.
* **Sin reinicios de servicio:** La configuración se consulta en cada llamada, permitiendo actualizar prompts y parámetros en caliente sin re-desplegar la aplicación.

### 10.1. Claves Configurables

| Clave | Tipo | Descripción | Ejemplo / Valor Inicial |
| :--- | :--- | :--- | :--- |
| `system_prompt` | `texto` | Rol (asesor de ventas de AldeaZON), trato de usted y reglas de respuesta (ver §11.6) | `Eres el asesor de ventas de AldeaZON...` |
| `user_prompt_template` | `plantilla` | Formato del prompt de usuario. Requiere `{contexto}`, `{consulta}` y `{perfil}` | `CONTEXTO RECUPERADO:\n{contexto}\n\nCONSULTA DEL CLIENTE:\n{consulta}\n\nPERFIL DEL USUARIO:\n{perfil}` |
| `chunk_template` | `plantilla` | Formato de serialización de cada chunk recuperado de Qdrant | `[Documento: {documento_fuente} \| Producto: {producto}...]` |
| `msg_retrieval_error` | `texto` | Respuesta ante fallo de conexión con el motor vectorial Qdrant | `El servicio de consulta técnica está temporalmente indisponible...` |
| `msg_baja_confianza` | `texto` | Respuesta cuando la confianza de similitud no supera el umbral | `No tengo suficiente información respaldada en el corpus...` |
| `msg_llm_error` | `texto` | Respuesta ante fallo o timeout del proveedor LLM | `Error temporal de conexión con el generador técnico...` |
| `msg_groundedness_rechazo` | `texto` | Respuesta cuando el filtro de dosis detecta una alucinación numérica | `La dosis generada requiere verificación técnica...` |
| `llm_modelo` | `texto` | Modelo de OpenAI para generación | `gpt-4o-mini` |
| `llm_temperature` | `float` | Temperatura de muestreo del modelo (ej. `0.2`) | `0.2` |
| `llm_max_tokens` | `int` | Límite máximo de tokens de salida | `250` |
| `retrieval_top_k` | `int` | Cantidad de chunks a recuperar desde Qdrant | `3` |

Las claves del perfilamiento y del filtro de alcance están en la sección 11.10. Al editar `system_prompt`, `user_prompt_template`, `chunk_template`, `llm_modelo`, `llm_temperature` o `retrieval_top_k`, las respuestas cacheadas en Redis con la versión anterior dejan de usarse (la clave de caché incluye una huella de estas claves).

### 10.2. Actualización de Configuración

Para modificar cualquier parámetro en caliente:

```sql
-- Ejemplo: Modificar el system prompt
UPDATE atv.prompt_config
SET valor = 'Nuevo system prompt...',
    ts_actualizado = CURRENT_TIMESTAMP,
    actualizado_por = 'admin'
WHERE clave = 'system_prompt';

-- Ejemplo: Cambiar el modelo LLM y temperatura
UPDATE atv.prompt_config SET valor = 'gpt-4o', ts_actualizado = CURRENT_TIMESTAMP WHERE clave = 'llm_modelo';
UPDATE atv.prompt_config SET valor = '0.1', ts_actualizado = CURRENT_TIMESTAMP WHERE clave = 'llm_temperature';
```
---

## 11\. El Agente: Perfilamiento, Flujo de Respuesta y Datos de Semillero

El ATV atiende a **cualquier persona desde la web de AldeaZON** (socio, cliente, productor, comerciante o técnico) como un **asesor de ventas de AldeaZON con conocimiento técnico de café y de los productos**. Antes de recomendar, **siempre** averigua quién pregunta y cómo es su finca o negocio, y adapta la respuesta a ese perfil.

### 11.1. Vista general

```mermaid
flowchart TD
    W[Web AldeaZON<br/>POST /api/v1/consultar] --> S{¿Sesión completa?<br/>atv.sesion_perfil}
    S -- no --> P[Perfilamiento<br/>perfilamiento.py]
    P --> I{¿Cédula + WhatsApp?}
    I -- sí --> H[HMAC-SHA256 con el salt de Semillero<br/>busca en atv.perfil_productor]
    H -- coincide --> R[Perfil del registro:<br/>rubros, fincas, lotes, zona]
    H -- no coincide --> X
    I -- no --> X[LLM extrae lo declarado<br/>perfil_extraccion_prompt]
    R --> F{¿Falta algún dato?}
    X --> F
    F -- sí --> Q[Devuelve la siguiente pregunta<br/>etapa = perfilamiento]
    F -- no --> G
    S -- sí --> G[RAG con perfil<br/>rag_service.py]
    G --> FQ[Candidatas de preguntas frecuentes<br/>embedding contra atv.pregunta_frecuente]
    FQ --> A{Filtro de alcance + juez FAQ<br/>en paralelo}
    A -- es una pregunta frecuente --> RF[Respuesta aprobada por Asistencia Técnica<br/>adaptada por el LLM]
    A -- fuera --> FA[msg_fuera_alcance]
    A -- dentro, no es FAQ --> C[Fichas técnicas: caché → Qdrant → umbral → LLM →<br/>abstención → groundedness → carrito]
    C --> RES[Respuesta etapa = respuesta]
    RF --> RES

    subgraph Datos diarios
      M[(VPS1 MariaDB<br/>ssp / catalogos)] -- Semillero 20:00 y 23:30 --> STG[(semillero.stg_cafe<br/>fotos diarias, PII hasheada)]
      STG -- DAG atv_perfil_productor 01:30 --> AP[(staging.atv.perfil_*)]
    end
    AP -.-> H
```

### 11.2. Contrato de la API

`POST /api/v1/consultar` (header `X-API-Key`). La web guarda el `sesion_id` de la primera respuesta y lo reenvía en cada mensaje.

| Campo request | Obligatorio | Descripción |
| --- | --- | --- |
| `consulta` | sí | Texto del usuario (2 a 1.000 caracteres) |
| `sesion_id` | no | Vacío = sesión nueva; la API genera uno y lo devuelve |
| `sucursal_id` | no | Sucursal de AldeaZON para el carrito. Si falta, se usa la del socio identificado |
| `vendedor_id` | no | `"web"` por defecto (o el id del vendedor en tienda) |

| Campo response | Descripción |
| --- | --- |
| `respuesta` | Pregunta de perfilamiento o respuesta técnica |
| `etapa` | `perfilamiento` (el agente está preguntando) o `respuesta` (respuesta técnica del RAG) |
| `sesion_id` | Id a reenviar en el siguiente mensaje |
| `perfil` | Perfil acumulado, **sin datos personales** (sin `persona_id`, cédula, teléfono ni coordenadas) |
| `fuente` | Ficha técnica principal y versión |
| `escalada_hitl` | `true` si la consulta debe pasar a Asistencia Técnica |
| `carrito` | Productos sugeridos con stock (vacío si escaló o no es recomendación) |
| `latencia_ms` | Tiempo total |

### 11.3. Paso a paso de un mensaje

`app/main.py` → `procesar_consulta_interna`:

1. **Configuración.** Lee `atv.prompt_config` (`cargar_prompt_config`). Si falta una clave o es inválida → **HTTP 503** y registro en `atv.error_log`.
2. **Perfilamiento** (`Perfilador.procesar_turno`, sección 11.4). Si todavía falta información devuelve la siguiente pregunta (`etapa = perfilamiento`) y termina el turno. El estado de la sesión se guarda (commit) **antes** de pasar al RAG.
3. **RAG** (`ATVRagService.procesar_consulta`, sección 11.5) con la consulta pendiente o el mensaje actual y el perfil.
4. **Trazabilidad.** Cada respuesta del RAG se registra en `atv.consulta` (texto sanitizado, intención, confianza, tokens, costo, latencias por fase, productos sugeridos); los errores, en `atv.error_log`. Los turnos de perfilamiento no van a `atv.consulta`: su rastro queda en `atv.sesion_perfil`.

### 11.4. Perfilamiento (`app/services/perfilamiento.py`, `app/services/perfil_cliente.py`)

**Orden de preguntas** (cada dato se pregunta **una sola vez**; como máximo `perfil_max_preguntas` = 4 preguntas, después responde con lo que haya):

| Turno | Pregunta | Clave en `atv.prompt_config` | Se omite si… |
| --- | --- | --- | --- |
| 1 (sesión nueva) | ¿Es socio/cliente de Aldea Global? Cédula + WhatsApp. Si no, ¿a qué se dedica? | `perfil_msg_bienvenida` | el primer mensaje ya identifica a un socio |
| — | Pide el WhatsApp si solo escribió la cédula | `perfil_msg_pedir_telefono` | — |
| si falta | Cultivos y manzanas | `perfil_msg_pedir_cultivo` | el registro trae rubros |
| si falta | Municipio o departamento | `perfil_msg_pedir_ubicacion` | el registro trae zona |
| opcional | Etapa del cultivo y terreno (plano/quebrado, suelo, altura) | `perfil_msg_pedir_terreno` | tiene finca registrada, es comerciante o técnico, o ya lo dijo |
| fin | Responde la consulta técnica hecha en el camino; si no hubo, pide la consulta | `perfil_msg_completo` | — |

**Identificación de socios y clientes**

* La cédula (`001-010190-0000A`, con o sin guiones) y el teléfono (8 dígitos, acepta `+505`, guiones y espacios) se leen del **texto crudo** y se convierten de inmediato en **HMAC-SHA256 con el salt de Semillero** (`SEMILLERO_HASH_SALT`), igual que `core/masking.py` de Semillero. Se prueban las variantes de formato con que la cédula puede estar en el origen (mayúscula/minúscula, con/sin guiones).
* Se exige **cédula y teléfono**: solo con la cédula nadie puede ver la finca de otra persona. Máximo **3 intentos** por sesión (frena la enumeración de cédulas).
* Si coinciden, el perfil sale del registro (`fuente = registro`, `verificado = true`) y el agente confirma: *"Gracias, encontré su registro. Tengo esta información: socio activo de Aldea Global; 1 finca(s) registrada(s); en San Rafael del Norte, Jinotega."* (`perfil_msg_verificado`, placeholder `{resumen}`).
* Si no coinciden o no hay salt: `perfil_msg_no_encontrado` y sigue con preguntas.
* La sesión guarda los hashes de la cédula solo mientras espera el teléfono; después los borra.

**Lo que dice el usuario.** Cada mensaje (ya sanitizado) pasa por un LLM con `perfil_extraccion_prompt` que devuelve JSON: `tipo_usuario` (`socio`, `cliente`, `productor`, `comerciante`, `tecnico`, `otro`), `cultivos` (nombre, área, unidad `mz`/`ha`; las hectáreas se convierten a manzanas: 1 mz = 0,7026 ha), `municipio`, `departamento`, `etapa_cultivo`, `topografia`, `tipo_suelo`, `altura_msnm`, `tipo_negocio` y `consulta_tecnica` (la pregunta agronómica, que queda pendiente hasta completar el perfil). Lo declarado nunca pisa lo del registro. Si el LLM falla, el turno sigue y se vuelve a preguntar.

**Qué recibe el LLM de generación** (`{perfil}` en `user_prompt_template`), por ejemplo:

```
- Quién pregunta: socio activo de Aldea Global
- Cultivos: Café (7.4 mz), Frijol (2 mz)
- Ubicación: Jinotega
- Producción orgánica: sí (priorizar productos permitidos en agricultura orgánica)
- Finca 1: 12 mz en total, 7.5 mz de café, 1150 msnm, pendiente 30 %, topografía SemiQuebrado, suelo Franco Arcilloso
  - Lote: 2.5 mz, variedad Catuai, etapa Productivo Selectivo, sembrado en 2019
```

Hasta 3 fincas y 5 lotes por finca. Nunca incluye nombres, cédula, teléfonos, coordenadas ni `persona_id`.

### 11.5. Pipeline de respuesta (`app/services/rag_service.py`)

| # | Fase | Qué hace | Si falla o no pasa |
| --- | --- | --- | --- |
| 1 | Sanitización Ley 787 | `sanitizar_pii`: cédula → `[CEDULA]`, teléfono → `[TELEFONO]`, nombres (spaCy) → `[NOMBRE]` | — |
| 2 | Intención | Regex de riesgo (`dosis`, `qué producto`, `aplicar`, `l/ha`…) → `recomendacion`; si no, `informativa` | — |
| 3a | **Preguntas frecuentes: candidatas** | Las `faq_candidatos` (5) preguntas de `atv.pregunta_frecuente` más parecidas a la consulta (embedding de la pregunta y sus variantes, en memoria) | Tabla no disponible → sigue sin FAQ |
| 3b | **Filtro de alcance** ∥ **juez FAQ** | Dos llamadas en paralelo: `alcance_prompt` → `{"en_alcance": bool}` y `faq_juez_prompt` → `{"codigo": "FAQ-xxx" \| null}` (¿pregunta **lo mismo** que una candidata, con cultivo compatible?) | Fuera de alcance y sin FAQ → `msg_fuera_alcance`. El filtro falla → **deja pasar**; el juez falla → **sin FAQ** (fichas) |
| 3c | **Respuesta de la FAQ** | Si el juez eligió una: busca también las fichas (para el producto) y genera con `system_prompt` + `faq_user_prompt_template` (respuesta aprobada, lo que hay que evitar, `{productos}`); el **selector de producto** corre en paralelo (ver fase 7.5). Fuente `Preguntas frecuentes de Asistencia Técnica, FAQ-xxx`; no escala, sin carrito, **termina aquí** | LLM caído, abstención o dosis que no está en la respuesta aprobada → se entrega la **respuesta aprobada literal** (`modelo = faq_literal`) |
| 4 | Caché Redis | Solo consultas `informativa`. Clave = consulta + perfil + **huella de los prompts** (editar `prompt_config` invalida lo cacheado). TTL 7 días | — |
| 5 | Búsqueda Qdrant | Embedding `multilingual-e5-large` de la consulta **+ cultivo principal del perfil**; `retrieval_top_k` fichas | Error → `msg_retrieval_error`, escala |
| 6 | Umbral de confianza | Score ≥ **0,70** (recomendación) / **0,55** (informativa) | Por debajo → `msg_baja_confianza`, escala sin llamar al LLM |
| 7 | Generación | `system_prompt` + `user_prompt_template` (`{contexto}`, `{consulta}`, `{perfil}`), `llm_modelo`, timeout 6 s | Error → `msg_llm_error`, escala |
| 8 | Abstención | Si la respuesta dice *"No tengo esa información en las fichas vigentes…"* → escala a Asistencia Técnica | — |
| 9 | Groundedness | Toda dosis de la respuesta (`x L/ha`, `kg/mz`…) debe estar **literal** en las fichas recuperadas | Dosis inventada → `msg_groundedness_rechazo`, escala |
| 7.5 | **Producto AldeaZON** (`producto_service.py`) | En paralelo con la generación, `producto_selector_prompt` elige entre las fichas recuperadas la del **mismo problema y cultivo** (para plagas/enfermedades/malezas el uso de la ficha tiene que nombrar el problema; el tipo debe corresponder) → JSON `{"indice": n\|null}`. El **código** arma la línea `producto_linea_template` con producto, uso y dosis literal de la ficha, o `msg_producto_tienda` si ninguna sirve, y la pone antes de la fuente (si el LLM escribió una, se quita). También en la FAQ y en su respuesta literal | Abstención, rechazo o error: sin línea. Selector caído → mensaje de la tienda |
| 10 | Carrito | Solo `recomendacion` con sucursal: productos con el ingrediente activo de la ficha top-1, `estado = 1`, stock > 0, del más barato al más caro. **Precio de asociado solo para socios activos verificados** | Error de catálogo → carrito vacío (la respuesta técnica sale igual) |
| 11 | Guardar en caché | Solo `informativa` | — |

### 11.6. Reglas para responder

Viven en `atv.prompt_config` (`scripts/004_prompt_vendedor.sql`), se editan en caliente y se aplican desde la siguiente consulta:

* **Rol:** asesor de ventas de AldeaZON, con conocimiento de café y de los productos, atendiendo a un cliente.
* **Trato:** siempre de **usted**; nunca tutea ("puede", "aplique", "valide"; nunca "puedes", "aplica", "valida"). Cordial y profesional, máximo ~120 palabras.
* **Primero las preguntas frecuentes:** si la consulta es una de las preguntas aprobadas por Asistencia Técnica (`atv.pregunta_frecuente`), se responde con esa respuesta (puede ordenarla y adaptarla al perfil, no contradecirla ni agregar productos o dosis) y se cita `FAQ-xxx`. Solo si no está en la lista se buscan las fichas técnicas.
* **Alcance:** solo agro y temas de Aldea Global / AldeaZON (lo decide el filtro de la fase 3, no el LLM de generación: con `gpt-4o-mini` esa decisión dentro del mismo prompt rechazaba preguntas agrícolas cuyas fichas no coincidían).
* **Solo con las fichas:** responde únicamente con el CONTEXTO; si no está, dice exactamente *"No tengo esa información en las fichas vigentes. Consulte al Área de Asistencia Técnica."*
* **Nunca inventa** dosis, productos ni condiciones; toda cifra debe estar literal en las fichas.
* **Fuente obligatoria** al cierre: `[Fuente: documento, versión v]`.
* **Cierre con producto AldeaZON** (antes de la fuente): no lo escribe el LLM; lo elige el selector y lo arma el código con los datos de la ficha (fase 7.5). Nunca tras la abstención. Historia: con la regla en el prompt (scripts 006–008) gpt-4o-mini recomendó un herbicida para mosca blanca y, endurecida, dejó de recomendar productos que sí servían; 009–010 la pasaron al sistema.
* **Recomendaciones:** incluye *"Valide esta recomendación con Asistencia Técnica antes de aplicarla."* (esta frase **no** escala la consulta).
* **Perfil:** adapta cultivo, etapa, terreno, zona y tipo de cliente; si el perfil no alcanza, lo dice y sugiere Asistencia Técnica. Nunca menciona datos personales.

**Cuándo escala a Asistencia Técnica (`escalada_hitl = true`, carrito vacío):** baja confianza en la búsqueda, error de Qdrant o del LLM, abstención ("No tengo esa información…") o dosis no respaldada por la ficha.

### 11.7. Tablas de Semillero usadas

Todas se leen de la **última foto** (`max(_fecha_carga)`) del esquema `stg_cafe`, base **`semillero`** del PostgreSQL 10.0.0.14, con el rol `ostendata` (solo `SELECT`). Semillero aplica el enmascaramiento al ingerir: lo que figura como *hash* llega como HMAC-SHA256.

| Tabla `stg_cafe` | Origen | Módulo / catálogo Semillero | Columnas usadas por el ATV | Enmascarado por Semillero |
| --- | --- | --- | --- | --- |
| `persona` | `ssp.persona` | `cosecha` (ya existía) | `id`, `cedula`, `whatsapp_claro`, `whatsapp_tigo`, `comunidad_id`, `sucursal_id`, `cafetalero` | hash: `nombre`, `cedula`, `foto`, `whatsapp_*`; `fecha_nac` generalizada |
| `asociado` | `ssp.asociado` | `asociados` (ya existía) | `persona_id`, `estado_asociado`, `sucursal_id`, `es_organico`, `cafetalero`, `numero_mz` | hash: beneficiarios y sus cédulas; lat/long generalizadas |
| `cliente` | `ssp.cliente` | `cosecha` (ya existía) | `id` (= persona.id), `tipo_cliente`, `celular` | hash: `celular`, `url_cedula`, `verificado_por` |
| `comunidad` | `ssp.comunidad` | `cosecha` (ya existía) | `id`, `nombre`, `distrito_id` | — |
| `finca` | `ssp.finca` | `perfil_finca`, id 135 | `id`, `asociado_persona_id`, `comunidad_id`, `altura`, `porc_pendiente`, `topografia`, `tipo_suelo`, áreas (`total_area_finca`, `area_cafe_*`, granos, frutas, pastos, bosque), `fecha_floracion`, `activo` | hash: `nombre`, `coordenadas`, `colindante_*`, `observaciones`, `creadoPor`, `actualizadoPor` |
| `finca_lote` | `ssp.finca_lote` | `perfil_finca`, id 136 | `id`, `finca_id`, `nombre`, `area`, `fecha_siembra`, `plantas_x_mz`, `variedad_id`, `estado_id`, `rendimiento_esperado`, `activo` | — |
| `finca_lote_estado` | `ssp.finca_lote_estado` | `perfil_finca`, id 137 | `id`, `nombre` (etapa del lote) | — |
| `catalogo_variedad_cafe` | `ssp.catalogo_variedad_cafe` | `perfil_finca`, id 138 | `id`, `nombre` | — |
| `asociado_mz_rubro` | `ssp.asociado_mz_rubro` | `perfil_finca`, id 139 | `asociado_id`, `rubro`, `mz_avg` | — |
| `distrito` | `ssp.distrito` | `perfil_finca`, id 140 | `id`, `municipio_id` | — |
| `municipio` | `ssp.municipio` | `perfil_finca`, id 141 | `id`, `nombre`, `departamento_id` | — |
| `ssp_departamento` | `catalogos.ssp_departamento` | `perfil_finca`, id 142 | `id`, `nombre` | — |

Relaciones: `persona.id` = `asociado.persona_id` = `cliente.id` = `finca.asociado_persona_id` = `asociado_mz_rubro.asociado_id`; `finca_lote.finca_id` → `finca.id`; `comunidad.distrito_id` → `distrito.municipio_id` → `municipio.departamento_id` → `ssp_departamento.id`.

Notas de datos (medido el 2026-10-15): en producción **no existe `ssp.departamento`** (solo en la copia de develop), por eso se usa `catalogos.ssp_departamento`; `asociado_mz_rubro` no tiene PK en el origen (`asociado_id, rubro_id` es único); solo 2 de 1.835 lotes traen variedad.

El registro en el catálogo está en `scripts/semillero/004_cafe_perfil_finca.sql` (aplicado en `semillero_control`). Lote de ingesta: `semillero_ingesta_cafe_perfil_finca_01`, diario a las 20:00 de Managua; `persona`, `cliente` y `comunidad` cargan con el módulo `cosecha` a las 23:30.

### 11.8. Tablas del ATV (`staging.atv`, `scripts/003_create_perfil.sql`)

| Tabla | Contenido | Escribe | Lee |
| --- | --- | --- | --- |
| `atv.perfil_productor` | Una fila por persona: `cedula_hash`, `telefono_hashes[]`, socio/estado, `tipo_cliente`, sucursal, orgánico, manzanas, comunidad/municipio/departamento, `rubros` (JSON) y `fecha_foto` | DAG | API (por `cedula_hash` indexado) |
| `atv.perfil_finca` | Fincas activas: altura, pendiente, topografía, suelo, áreas, municipio/departamento | DAG | API |
| `atv.perfil_lote` | Lotes activos: área, variedad, etapa, siembra, densidad, rendimiento esperado | DAG | API |
| `atv.pregunta_frecuente` | Preguntas frecuentes de Asistencia Técnica (`scripts/005_create_pregunta_frecuente.sql`): `codigo`, cultivo, categoría, pregunta, `variantes[]`, respuesta aprobada, `respuesta_evitar`, `activo` | `scripts/cargar_preguntas_frecuentes.py` (desde el Excel) | API (embeddings en memoria; se recalculan solos al cambiar la tabla) |
| `atv.sesion_perfil` | Estado por sesión: `estado` (`perfilando`/`completo`), `perfil` (JSON), preguntas hechas, intentos de identificación, consulta pendiente (sanitizada) | API | API |

La primera carga (2026-10-15) dejó 39.887 personas (22.565 socios activos), 6.304 fincas y 1.835 lotes.

### 11.9. Carga diaria: DAG `atv_perfil_productor`

* `dags/dag_perfil_productor.py`, en el Airflow de VPS2 (`semillero_airflow`), **01:30 America/Managua**, después de las cargas de Semillero. Una tarea que hace SSH a VPS4 y ejecuta `python -m scripts.airflow_tasks construir_perfiles` (`scripts/construir_perfiles.py`).
* Lee la última foto de las 12 tablas de `stg_cafe` (base `semillero`) y reescribe `atv.perfil_*` (base `staging`) en **una sola transacción** (`DELETE` + `INSERT`: la API sigue leyendo los perfiles anteriores hasta el commit).
* **Falla sin tocar los perfiles vigentes** si alguna tabla no tiene fotos, si su última foto tiene más de **2 días**, o si el número de personas baja a menos de la **mitad**. Reintenta 2 veces cada 15 minutos.
* Corrida manual: `python -m scripts.airflow_tasks construir_perfiles` en VPS4. Publicar cambios del DAG: `python scripts/sync_dag_airflow.py --archivo dags/dag_perfil_productor.py --force`.

### 11.10. Configuración

**`.env` (VPS4, permisos `600`):**

| Variable | Uso |
| --- | --- |
| `SEMILLERO_PG_HOST`, `SEMILLERO_PG_PORT`, `SEMILLERO_PG_DB`, `SEMILLERO_PG_USER`, `SEMILLERO_PG_PASSWORD` | Conexión de solo lectura a la base `semillero` (la arma `settings.semillero_database_url`; la contraseña se escapa sola) |
| `SEMILLERO_HASH_SALT` | Salt de `hash_determinista` de Semillero (Variable de Airflow `semillero_masking_salt`). Sin él no se identifican socios. Si Semillero lo cambia, hay que actualizarlo aquí: si no coinciden, ningún socio se reconoce (sin error visible) |

**Claves de `atv.prompt_config` agregadas por el agente** (además de las de la sección 10.1):

| Clave | Tipo | Uso |
| --- | --- | --- |
| `perfil_msg_bienvenida`, `perfil_msg_pedir_telefono`, `perfil_msg_no_encontrado`, `perfil_msg_pedir_cultivo`, `perfil_msg_pedir_ubicacion`, `perfil_msg_pedir_terreno`, `perfil_msg_completo` | texto | Preguntas y mensajes del perfilamiento |
| `perfil_msg_verificado` | plantilla | Confirmación al socio identificado; `{resumen}` obligatorio |
| `perfil_extraccion_prompt` | texto | Extractor JSON de lo que declara el usuario |
| `perfil_max_preguntas` | int | Tope de preguntas de perfil (4) |
| `alcance_prompt` | texto | Filtro de alcance (JSON `{"en_alcance": bool}`) |
| `msg_fuera_alcance` | texto | Respuesta a consultas fuera de alcance |
| `faq_juez_prompt` | texto | Juez de preguntas frecuentes (JSON `{"codigo": str\|null}`) |
| `faq_user_prompt_template` | plantilla | Mensaje al LLM con la respuesta aprobada; `{respuesta}`, `{consulta}`, `{perfil}` obligatorios; `{codigo}`, `{pregunta}`, `{evitar}` permitidos |
| `producto_selector_prompt` | texto | Selector del producto que cierra la respuesta (JSON `{"indice": int\|null}`) |
| `producto_linea_template` | plantilla | Línea de cierre; `{producto}` obligatorio, `{uso}`, `{dosis}`, `{documento}` permitidos |
| `msg_producto_tienda` | texto | Cierre cuando ninguna ficha sirve |
| `faq_candidatos` | int | Candidatas que recibe el juez (5); `0` desactiva las preguntas frecuentes |

`user_prompt_template` exige además el placeholder `{perfil}`.

### 11.11. Despliegue y operación

Orden usado en producción (2026-10-15 / 17):

1. **Semillero:** `scripts/semillero/004_cafe_perfil_finca.sql` en `semillero_control` → `semillero_preparar_destino` (previsualizar y aplicar) → despausar `semillero_ingesta_cafe_perfil_finca_01`.
2. **PostgreSQL 10.0.0.14:** regla `pg_hba.conf` para VPS4 → base `semillero` y `GRANT SELECT` de las 12 tablas al rol del ATV (instrucciones para el admin: `docs/instrucciones_admin_vps2_perfil_atv.pdf`).
3. **ATV:** `scripts/003_create_perfil.sql` y `scripts/004_prompt_vendedor.sql` sobre `staging` **y reinicio inmediato** de `aldeazon-atv.service` (el código y las claves nuevas van juntos: el código viejo no conoce `{perfil}` y el nuevo exige las claves nuevas).
4. **Carga inicial** (`construir_perfiles`) y publicación del DAG en VPS2.
5. **Preguntas frecuentes:** `scripts/005_create_pregunta_frecuente.sql` sobre `staging` → `python -m scripts.cargar_preguntas_frecuentes "docs/Preguntas_y_Respuestas_AldeaZON rellenado.xlsx"` (primero con `--dry-run`) → reinicio de la API (código nuevo). Para **actualizar** la lista basta volver a correr el script con el Excel nuevo: hace upsert por ID, desactiva los IDs que ya no vienen y la API recalcula los embeddings en la siguiente consulta, sin reinicio. Se niega a cargar si el Excel trae menos de la mitad de las preguntas activas.

6. **Corpus de fichas v2 (extracción por ficha):** `python -m scripts.reindexar_corpus construir --version 2` llena `atv_corpus_v2` sin tocar el alias (la API sigue en v1); `probar --version 2 "consulta" ...` compara v1 y v2; `promover --version 2` actualiza `atv.inventario_archivo`, mueve el alias `atv_corpus` → `atv_corpus_v2` y vacía Redis. v1 no se borra: volver atrás = `promover --version 1`. Reporte por archivo en `fixtures/reindex_v2.json`. El build carga su propia copia de e5 (~2,1–2,4 GB de RSS): correrlo con la API ya liberada de la copia duplicada del modelo (`cache_service.py` ahora la carga solo si se usa) y vigilando la RAM.

**Extracción por ficha** (`app/services/extractor_ficha.py`, desde v2; la ingesta diaria la usa cuando el alias apunta a v2 o más, ver `ingest_seaweedfs.version_vigente`): texto del PDF (o transcripción por visión si casi no tiene texto) → una llamada al LLM que devuelve producto, tipo, descripción, precauciones y todos los usos (cultivo, objetivo, dosis, condiciones) → **anclaje**: cada cifra de una dosis debe estar en la ficha y su unidad (`L/ha`, `ml`...) también; si no, la dosis se descarta y el uso queda sin cifra → un registro por uso + uno general por ficha. Las dosis sin unidad de área (por litro, bomba, planta) van a `condiciones` (`RegistroAtomico.dosis` sigue exigiendo /ha o /mz). Resultado al 2026-10-17: 141/142 fichas con registros, 962 registros (v1: 68 fichas, 152), 478 con dosis por ha/mz, 138 productos.

Comprobaciones útiles:

```sql
-- staging: frescura y volumen del perfil
SELECT max(fecha_foto), count(*), count(*) FILTER (WHERE es_asociado) FROM atv.perfil_productor;
-- staging: sesiones del día por estado
SELECT estado, count(*) FROM atv.sesion_perfil WHERE ts_creado::date = current_date GROUP BY 1;
-- staging: qué preguntas frecuentes se están usando
SELECT registros_usados->0->>'codigo' AS faq, count(*) FROM atv.consulta
 WHERE modelo LIKE '%faq%' AND ts::date = current_date GROUP BY 1 ORDER BY 2 DESC;
-- staging: consultas fuera de alcance y escaladas
SELECT intencion, escalada_hitl, count(*) FROM atv.consulta WHERE ts::date = current_date GROUP BY 1, 2;
```

### 11.12. Limitaciones conocidas

* **Carrito vacío:** `catalog.py` consulta `stc2.agroquimico.ingrediente_activo` y `stc2.az_bodega_inventario`, pero esas tablas no están en la base `staging` y en el origen `agroquimico` no tiene `ingrediente_activo`. Falta definir de dónde sale el ingrediente activo y traer el catálogo por Semillero (proyecto `insumos`).
* **Turnos de perfilamiento** no quedan en `atv.consulta` (solo en `atv.sesion_perfil`).
* **Una vez completo**, el perfil de la sesión ya no se actualiza con lo que el usuario diga después (se responde con el perfil acumulado).
* **Búsqueda de fichas:** el cultivo del perfil se suma ("… en café") solo si la consulta no nombra un cultivo ni un producto del corpus (`texto_de_busqueda`). La forma anterior (" Cultivo: café.") traía fertilizantes en vez de los fungicidas de roya con el corpus v2.
* **Calidad de la extracción v2:** hay usos con el nombre científico del cultivo como "objetivo" (p. ej. KNIGHT 72 SC → "Coffea arabica") o cultivos mal leídos ("Café de zacate" en RUMBA 28 SC). Los registros están `en_revision`; conviene que Asistencia Técnica revise `fixtures/reindex_v2.json`.
* El filtro de alcance y el juez de preguntas frecuentes (en paralelo) agregan ~0,5 s por consulta.
* **Pregunta frecuente vs. dosis puntual:** el juez tiende a asignar a la FAQ general consultas más específicas (p. ej. *"¿qué dosis de Opera por manzana para roya?"* → FAQ-001, que no da dosis y pide confirmar la roya). La respuesta es segura pero no busca la dosis en la ficha.
* **Corpus v1** (extracción por línea, `extractor.py`): solo guardaba líneas con un cultivo de una lista fija de 14 y una dosis x/ha|mz en la misma línea; 74 de 142 fichas quedaron en ERROR (fertilizantes, tablas partidas, dosis por litro). Lo corrige v2 (ver 11.11, paso 6). `AF YaraBela NITROMAG 27_Costa Rica.pdf` está dañado en SeaweedFS (no es un PDF válido).
* Las columnas *Dato crítico*, *Nivel de riesgo*, *Producto(s)* y *Fuente* del Excel vienen vacías: se cargan, pero hoy no aportan nada a la respuesta.
