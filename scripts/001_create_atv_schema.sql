-- scripts/001_create_atv_schema.sql
-- TARGET: PostgreSQL 18.4 (VPS3 - atv_qa / atv_prod)
-- COMPLIANCE: Ley 787 (Nicaraguan Data Privacy) - Sanitized PII only.

CREATE SCHEMA IF NOT EXISTS atv;

-- 1. ENUM DEFINITIONS
CREATE TYPE atv.intencion_tipo AS ENUM ('informativa', 'recomendacion', 'fuera_alcance');

-- 2. TRACEABILITY & AUDIT LOG TABLE (Runtime Serving Telemetry)
CREATE TABLE IF NOT EXISTS atv.consulta (
    consulta_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ts TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    -- LEY 787 COMPLIANCE:
    -- We persist SHA-256 hashes of seller IDs and Regex/NER scrubbed prompt text.
    -- Unencrypted PII ([CEDULA], [TELEFONO], [NOMBRE]) is never written to disk.
    vendedor_hash VARCHAR(64) NOT NULL,
    consulta_sanitizada TEXT NOT NULL,
    
    intencion atv.intencion_tipo NOT NULL,
    confianza NUMERIC(4, 3) NOT NULL CHECK (confianza >= 0.000 AND confianza <= 1.000),
    escalada_hitl BOOLEAN NOT NULL DEFAULT FALSE,
    respuesta TEXT,
    modelo VARCHAR(64),
    
    -- Token Economics
    tokens_entrada INTEGER NOT NULL DEFAULT 0,
    tokens_salida INTEGER NOT NULL DEFAULT 0,
    costo_estimado_usd NUMERIC(10, 6) NOT NULL DEFAULT 0.000000,
    
    -- Micro-Latency Instrumentation (APM Budget SLA: <= 10,000 ms)
    lat_webhook_ms INTEGER NOT NULL DEFAULT 0,
    lat_sanitizacion_ms INTEGER NOT NULL DEFAULT 0,
    lat_clasif_ms INTEGER NOT NULL DEFAULT 0,
    lat_retrieval_ms INTEGER NOT NULL DEFAULT 0,
    lat_llm_ms INTEGER NOT NULL DEFAULT 0,
    lat_total_ms INTEGER NOT NULL,
    
    -- Generated column to flag SLA breaches for Superset alerting
    sla_cumplido BOOLEAN GENERATED ALWAYS AS (lat_total_ms <= 10000) STORED,
    
    cache_hit BOOLEAN NOT NULL DEFAULT FALSE,
    registros_usados JSONB,
    version_corpus INTEGER NOT NULL,
    
    -- Commercial Integration Traceability (RSK-NEW-04)
    -- Stores array of stc2.agroquimico.id values suggested in the cart
    productos_sugeridos_ids INTEGER[] DEFAULT '{}'
);

-- 3. SEAWEEDFS FILE INVENTORY (Airflow Ingestion State)
CREATE TABLE IF NOT EXISTS atv.inventario_archivo (
    archivo_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nombre_archivo VARCHAR(255) NOT NULL UNIQUE,
    etag_seaweedfs VARCHAR(128) NOT NULL,
    version_corpus INTEGER NOT NULL,
    estado VARCHAR(32) NOT NULL CHECK (estado IN ('NUEVO', 'MODIFICADO', 'RETIRADO', 'INDEXADO', 'ERROR')),
    metodo_extraccion VARCHAR(32) NOT NULL DEFAULT 'columnas',
    total_registros_atomicos INTEGER NOT NULL DEFAULT 0,
    ts_cargado TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ts_indexado TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS atv.error_log
(
    error_id uuid NOT NULL,
    ts timestamp with time zone NOT NULL,
    consulta_id uuid,
    componente character varying(64) COLLATE pg_catalog."default" NOT NULL,
    excepcion_tipo character varying(128) COLLATE pg_catalog."default" NOT NULL,
    mensaje text COLLATE pg_catalog."default" NOT NULL,
    stack_trace text COLLATE pg_catalog."default",
    resuelto boolean NOT NULL,
    CONSTRAINT error_log_pkey PRIMARY KEY (error_id),
    CONSTRAINT error_log_consulta_id_fkey FOREIGN KEY (consulta_id)
        REFERENCES atv.consulta (consulta_id) MATCH SIMPLE
        ON UPDATE NO ACTION
        ON DELETE SET NULL
) TABLESPACE pg_default;

CREATE INDEX IF NOT EXISTS idx_error_componente_ts
    ON atv.error_log USING btree
    (componente COLLATE pg_catalog."default" ASC NULLS LAST, ts ASC NULLS LAST)
    TABLESPACE pg_default;

-- 4. HIGH-PERFORMANCE SUPERSET BI INDEXES
-- Covering index for temporal slicing and HITL escalation dashboards
CREATE INDEX IF NOT EXISTS idx_consulta_ts_hitl 
    ON atv.consulta (ts DESC, escalada_hitl);

-- Optimized index for intent distribution and model confidence analysis
CREATE INDEX IF NOT EXISTS idx_consulta_intencion_confianza 
    ON atv.consulta (intencion, confianza);

-- Partial index for SLA breach alerting (lat_total_ms > 10000)
CREATE INDEX IF NOT EXISTS idx_consulta_sla_breach 
    ON atv.consulta (ts DESC, lat_total_ms) 
    WHERE sla_cumplido = FALSE;

-- GIN index for deep JSONB queries against retrieved Qdrant chunk metadata
CREATE INDEX IF NOT EXISTS idx_consulta_registros_jsonb 
    ON atv.consulta USING GIN (registros_usados);