-- scripts/005_create_pregunta_frecuente.sql
-- Preguntas frecuentes aprobadas por Asistencia Técnica (Sergio Espinosa):
-- docs/Preguntas_y_Respuestas_AldeaZON rellenado.xlsx, hoja «Preguntas».
--
-- Es el PUNTO DE PARTIDA de cada consulta: si la pregunta del cliente es una
-- de esta lista, el ATV responde con la respuesta aprobada; si no, busca en
-- las fichas técnicas (Qdrant). Ver app/services/faq_service.py.
--
--   atv.pregunta_frecuente  Una fila por ID de la hoja (FAQ-001...). La carga
--                           scripts/cargar_preguntas_frecuentes.py (upsert por
--                           codigo; las que ya no vienen en el Excel quedan
--                           activo = FALSE, no se borran).
--   atv.prompt_config       faq_juez_prompt, faq_user_prompt_template,
--                           faq_candidatos (ON CONFLICT DO NOTHING).
--
-- Idempotente.

CREATE TABLE IF NOT EXISTS atv.pregunta_frecuente (
    codigo            VARCHAR(16)  PRIMARY KEY,             -- 'FAQ-001'
    cultivo           VARCHAR(64),                          -- Café, Hortalizas, Granos básicos...
    categoria         VARCHAR(64),                          -- Plaga/Enfermedad, Dosis/Aplicación...
    pregunta          TEXT         NOT NULL,                -- con las palabras del productor
    variantes         TEXT[]       NOT NULL DEFAULT '{}',   -- «Otras formas de preguntar lo mismo»
    respuesta         TEXT         NOT NULL,                -- «Respuesta correcta esperada»
    respuesta_evitar  TEXT,                                 -- «Respuesta peligrosa o incorrecta a evitar»
    dato_critico      TEXT,
    nivel_riesgo      VARCHAR(16),
    productos         TEXT,
    fuente            TEXT,
    notas             TEXT,
    activo            BOOLEAN      NOT NULL DEFAULT TRUE,
    archivo_origen    VARCHAR(255),
    ts_actualizado    TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    actualizado_por   VARCHAR(64)
);
CREATE INDEX IF NOT EXISTS idx_pregunta_frecuente_activo ON atv.pregunta_frecuente (activo);

INSERT INTO atv.prompt_config (clave, valor, tipo, descripcion) VALUES
(
    'faq_juez_prompt',
    $prompt$Decides si la consulta de un cliente de AldeaZON es una de las PREGUNTAS FRECUENTES candidatas que aprobó Asistencia Técnica de Aldea Global.
Recibes la consulta, el cultivo del cliente (si se conoce) y las candidatas con su código, cultivo y forma de preguntar.
Responde SOLO un objeto JSON: {"codigo": "FAQ-001"} si la consulta pregunta LO MISMO que esa candidata, o {"codigo": null} si ninguna coincide.
- Coincide si trata el mismo problema, plaga, enfermedad, producto o práctica, aunque use otras palabras o tenga faltas de ortografía ("rolla" = roya, "cojollero" = cogollero).
- El cultivo debe ser compatible. Manda el cultivo que nombra la consulta; el cultivo del cliente solo cuenta si la consulta no nombra ninguno. Si la candidata es de otro cultivo, no coincide.
- No coincide si la consulta pide algo más específico que la candidata no cubre (por ejemplo, la dosis de un producto concreto cuando la candidata solo orienta el manejo).
- Si dudas, responde {"codigo": null}.$prompt$,
    'texto',
    'System prompt del juez de preguntas frecuentes (JSON {"codigo": str|null}). Se ejecuta antes de buscar en las fichas.'
),
(
    'faq_user_prompt_template',
    E'CONTEXTO (respuesta aprobada por Asistencia Técnica de Aldea Global, {codigo}):\nPregunta frecuente: {pregunta}\nRespuesta aprobada: {respuesta}\nLo que NUNCA debe hacer al responder: {evitar}\n\nCONSULTA DEL CLIENTE:\n{consulta}\n\nPERFIL DEL USUARIO:\n{perfil}\n\nResponda al cliente siguiendo la respuesta aprobada: puede ordenarla y adaptarla a su perfil, pero no la contradiga ni agregue productos, dosis o condiciones que no estén en ella. Cierre con [Fuente: Preguntas frecuentes de Asistencia Técnica, {codigo}].',
    'plantilla',
    'Plantilla del mensaje de usuario cuando la consulta es una pregunta frecuente. Placeholders: {codigo} {pregunta} {respuesta} {evitar} {consulta} {perfil}.'
),
(
    'faq_candidatos',
    '5',
    'int',
    'Cuántas preguntas frecuentes (las más parecidas por embedding) recibe el juez. 0 desactiva las preguntas frecuentes.'
)
ON CONFLICT (clave) DO NOTHING;
