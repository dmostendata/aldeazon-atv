-- scripts/002_create_prompt_config.sql
-- Configuración de prompts y parámetros LLM del RAG (rag_service.py).
-- La BD es la única fuente de verdad: si falta una clave activa, la consulta falla (HTTP 503).
-- Idempotente: ON CONFLICT DO NOTHING no pisa ediciones hechas en producción.

CREATE TABLE IF NOT EXISTS atv.prompt_config (
    clave            VARCHAR(64) PRIMARY KEY,
    valor            TEXT        NOT NULL,
    tipo             VARCHAR(16) NOT NULL CHECK (tipo IN ('texto', 'plantilla', 'float', 'int')),
    descripcion      TEXT,
    activo           BOOLEAN     NOT NULL DEFAULT TRUE,
    ts_actualizado   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    actualizado_por  VARCHAR(64)
);

INSERT INTO atv.prompt_config (clave, valor, tipo, descripcion) VALUES
(
    'system_prompt',
    $prompt$Eres el Asistente Técnico Virtual de AldeaZON. Ayudas a vendedores de tiendas de insumos agrícolas en Nicaragua. Reglas obligatorias:
1. Responde SOLO con la información del CONTEXTO. Si el contexto no contiene la respuesta, di exactamente: 'No tengo esa información en las fichas vigentes. Consulta al Área de Asistencia Técnica.'
2. Nunca inventes dosis, productos ni condiciones. Toda cifra que menciones debe estar literalmente en el contexto.
3. Cierra SIEMPRE con la fuente: [Fuente: {documento}, versión {v}].
4. Responde en español, claro y breve. Máximo ~120 palabras.
5. Si la consulta pide una recomendación de producto o dosis, incluye: 'Valida esta recomendación con Asistencia Técnica antes de aplicarla.'$prompt$,
    'texto',
    'System prompt del Asistente Técnico Virtual. Se envía literal (sin .format()).'
),
(
    'user_prompt_template',
    E'CONTEXTO RECUPERADO:\n{contexto}\n\nCONSULTA DEL VENDEDOR:\n{consulta}',
    'plantilla',
    'Plantilla del mensaje de usuario. Placeholders obligatorios: {contexto}, {consulta}.'
),
(
    'chunk_template',
    '[Documento: {documento_fuente} | Producto: {producto} | Cultivo: {cultivo} | Dosis: {dosis} | Condiciones: {condiciones}]',
    'plantilla',
    'Formato de cada chunk recuperado de Qdrant. Placeholders: {documento_fuente}, {producto}, {cultivo}, {dosis}, {condiciones}.'
),
(
    'msg_retrieval_error',
    'El servicio de consulta técnica está temporalmente indisponible. Consulta con Asistencia Técnica.',
    'texto',
    'Respuesta al vendedor cuando falla la búsqueda en Qdrant.'
),
(
    'msg_baja_confianza',
    'No tengo suficiente información respaldada en el corpus para esta recomendación. Valida directamente con el Área de Asistencia Técnica.',
    'texto',
    'Respuesta cuando el score de Qdrant está bajo el umbral de confianza (HITL).'
),
(
    'msg_llm_error',
    'Error temporal de conexión con el generador técnico. Valida la dosis con Asistencia Técnica.',
    'texto',
    'Respuesta cuando falla la llamada al LLM.'
),
(
    'msg_groundedness_rechazo',
    'La dosis generada requiere verificación técnica. Consulta con el Área de Asistencia Técnica antes de aplicarla.',
    'texto',
    'Respuesta cuando el Groundedness Gate detecta una dosis no presente en el contexto.'
),
('llm_modelo',      'gpt-4o-mini', 'texto', 'Modelo OpenAI usado para la generación.'),
('llm_temperature', '0.2',         'float', 'Temperatura del LLM.'),
('llm_max_tokens',  '250',         'int',   'Máximo de tokens de salida del LLM.'),
('retrieval_top_k', '3',           'int',   'Cantidad de chunks recuperados de Qdrant.')
ON CONFLICT (clave) DO NOTHING;
