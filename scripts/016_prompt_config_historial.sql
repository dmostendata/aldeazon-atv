-- scripts/016_prompt_config_historial.sql
-- Historial de cambios de atv.prompt_config: cada edición desde la consola de
-- pruebas (tools/chat_streamlit, pestaña "Prompts del agente", vía
-- scripts/editar_prompt_config.py) guarda el valor anterior y el nuevo, quién y
-- cuándo, para poder ver qué cambió y volver atrás.
-- Sin reinicio. Idempotente.

CREATE TABLE IF NOT EXISTS atv.prompt_config_historial (
    id               BIGSERIAL    PRIMARY KEY,
    clave            VARCHAR(64)  NOT NULL REFERENCES atv.prompt_config (clave),
    valor_anterior   TEXT,
    valor_nuevo      TEXT         NOT NULL,
    autor            VARCHAR(64)  NOT NULL,
    motivo           TEXT,
    ts               TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_prompt_config_historial_clave ON atv.prompt_config_historial (clave, ts DESC);
