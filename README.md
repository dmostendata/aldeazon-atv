
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
    ├── scripts/
    │   └── ingest_sharepoint.py      # Offline synchronization & Qdrant collection builder
    ├── fixtures/                     # Local sample PDFs and test data
    ├── docker-compose.yml            # Local dev topology (Postgres 16, Qdrant 1.9.1, Redis 7)
    ├── requirements.txt              # Pinned Python ecosystem dependencies
    └── README.md                     # System documentation & architectural baseline