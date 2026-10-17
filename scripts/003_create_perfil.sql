-- scripts/003_create_perfil.sql
-- Perfil del usuario para el ATV: quién pregunta y qué finca tiene.
--
--   atv.perfil_productor / perfil_finca / perfil_lote
--       Copia desnormalizada y sin PII en claro de las fotos de stg_cafe
--       (Semillero, catálogo 004). La reconstruye cada madrugada el DAG
--       atv_perfil_productor (scripts/construir_perfiles.py). La API solo lee.
--       cedula_hash y telefono_hashes son el HMAC-SHA256 de Semillero
--       (mismo salt): la API hashea lo que escribe el usuario y compara.
--   atv.sesion_perfil
--       Estado de la conversación de perfilamiento por sesion_id de la web.
--       Ley 787: solo guarda hashes y texto sanitizado.
--
-- Idempotente. Las claves de prompt_config usan ON CONFLICT DO NOTHING; los
-- dos UPDATE del final AGREGAN texto a system_prompt y user_prompt_template
-- solo si todavía no lo tienen (no pisan ediciones hechas en producción).

CREATE TABLE IF NOT EXISTS atv.perfil_productor (
    persona_id        INTEGER PRIMARY KEY,
    cedula_hash       VARCHAR(64),
    telefono_hashes   TEXT[]      NOT NULL DEFAULT '{}',
    es_asociado       BOOLEAN     NOT NULL DEFAULT FALSE,
    estado_asociado   VARCHAR(10),
    tipo_cliente      VARCHAR(20),
    sucursal_id       INTEGER,
    es_organico       BOOLEAN,
    cafetalero        BOOLEAN,
    numero_mz         NUMERIC(16, 2),
    comunidad         VARCHAR(120),
    municipio         VARCHAR(45),
    departamento      VARCHAR(45),
    -- [{"rubro": "Café", "mz": 7.4}, ...] ordenado por manzanas desc
    rubros            JSONB       NOT NULL DEFAULT '[]',
    fecha_foto        DATE        NOT NULL,
    ts_construido     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_perfil_productor_cedula ON atv.perfil_productor (cedula_hash);

CREATE TABLE IF NOT EXISTS atv.perfil_finca (
    finca_id              INTEGER PRIMARY KEY,
    persona_id            INTEGER NOT NULL,
    altura_msnm           NUMERIC(10, 2),
    porc_pendiente        NUMERIC(10, 2),
    topografia            VARCHAR(50),
    tipo_suelo            VARCHAR(45),
    area_total_mz         NUMERIC(16, 2),
    area_cafe_mz          NUMERIC(16, 2),
    area_cafe_cosecha_mz  NUMERIC(16, 2),
    area_cafe_desarrollo_mz NUMERIC(16, 2),
    area_granos_basicos_mz NUMERIC(16, 2),
    area_frutas_mz        NUMERIC(16, 2),
    area_pastos_mz        NUMERIC(16, 2),
    area_bosque_mz        NUMERIC(16, 2),
    municipio             VARCHAR(45),
    departamento          VARCHAR(45),
    fecha_floracion       DATE
);
CREATE INDEX IF NOT EXISTS idx_perfil_finca_persona ON atv.perfil_finca (persona_id);

CREATE TABLE IF NOT EXISTS atv.perfil_lote (
    lote_id               INTEGER PRIMARY KEY,
    finca_id              INTEGER NOT NULL,
    nombre                VARCHAR(100),
    area_mz               NUMERIC(16, 2),
    variedad              VARCHAR(100),
    etapa                 VARCHAR(100),
    fecha_siembra         DATE,
    plantas_x_mz          INTEGER,
    rendimiento_esperado  NUMERIC(16, 2)
);
CREATE INDEX IF NOT EXISTS idx_perfil_lote_finca ON atv.perfil_lote (finca_id);

CREATE TABLE IF NOT EXISTS atv.sesion_perfil (
    sesion_id           VARCHAR(64) PRIMARY KEY,
    estado              VARCHAR(16) NOT NULL DEFAULT 'perfilando'
                          CHECK (estado IN ('perfilando', 'completo')),
    perfil              JSONB       NOT NULL DEFAULT '{}',
    persona_id          INTEGER,
    -- hashes de la cédula escrita mientras falta el teléfono (nunca la cédula)
    cedula_hashes       TEXT[]      NOT NULL DEFAULT '{}',
    intentos_identificacion INTEGER NOT NULL DEFAULT 0,
    preguntas_hechas    INTEGER     NOT NULL DEFAULT 0,
    preguntas           TEXT[]      NOT NULL DEFAULT '{}',
    consulta_pendiente  TEXT,       -- sanitizada (sanitizar_pii)
    ts_creado           TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ts_actualizado      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sesion_perfil_ts ON atv.sesion_perfil (ts_actualizado);


-- ---------------------------------------------------------------------
-- Textos del perfilamiento (app/services/perfilamiento.py)
-- ---------------------------------------------------------------------
INSERT INTO atv.prompt_config (clave, valor, tipo, descripcion) VALUES
(
    'perfil_msg_bienvenida',
    E'¡Hola! Soy el Asistente Técnico Virtual de AldeaZON. Para darle una recomendación adecuada a su finca, primero necesito conocerle un poco.\n\n¿Es usted socio o cliente de Aldea Global? Si lo es, escríbame su número de cédula y el número de WhatsApp que tiene registrado con nosotros. Si no lo es, cuénteme a qué se dedica (por ejemplo: productor, dueño de agroservicio, técnico).',
    'texto',
    'Primera respuesta de toda sesión nueva: pregunta quién es.'
),
(
    'perfil_msg_pedir_telefono',
    'Gracias. Para confirmar que es usted, escríbame también el número de WhatsApp que tiene registrado con Aldea Global.',
    'texto',
    'Cuando escribió la cédula pero no el teléfono.'
),
(
    'perfil_msg_no_encontrado',
    'No encontré un registro con esos datos. No se preocupe: le haré unas preguntas rápidas para conocer su finca.',
    'texto',
    'La cédula + teléfono no coinciden con ningún socio/cliente (o se agotaron los intentos).'
),
(
    'perfil_msg_verificado',
    'Gracias, encontré su registro. Tengo esta información: {resumen}.',
    'plantilla',
    'Socio/cliente identificado. Placeholder obligatorio: {resumen} (sin datos personales).'
),
(
    'perfil_msg_pedir_cultivo',
    '¿Qué cultivo o cultivos tiene y en cuántas manzanas aproximadamente?',
    'texto',
    'Pregunta por cultivos y área. A comerciantes/técnicos: cultivos que atienden.'
),
(
    'perfil_msg_pedir_ubicacion',
    '¿En qué municipio o departamento está su finca (o su negocio)?',
    'texto',
    'Pregunta por la ubicación.'
),
(
    'perfil_msg_pedir_terreno',
    'Para afinar la recomendación: ¿en qué etapa está el cultivo (siembra, desarrollo, floración, cosecha) y cómo es el terreno (plano o quebrado, tipo de suelo, altura aproximada)? Si no lo sabe, escriba «no sé».',
    'texto',
    'Pregunta opcional por etapa y terreno; se hace una sola vez y solo a productores sin finca registrada.'
),
(
    'perfil_msg_completo',
    'Perfecto, ya tengo lo necesario. ¿Cuál es su consulta técnica?',
    'texto',
    'Perfil completo y el usuario todavía no hizo ninguna pregunta técnica.'
),
(
    'perfil_extraccion_prompt',
    $prompt$Extraes datos del perfil de un usuario de una tienda de insumos agrícolas en Nicaragua a partir de su mensaje. Los datos personales ya vienen ocultos como [CEDULA], [TELEFONO] o [NOMBRE].
Responde SOLO un objeto JSON con estas claves (usa null o [] si el mensaje no lo dice; nunca inventes):
- "tipo_usuario": "socio" | "cliente" | "productor" | "comerciante" | "tecnico" | "otro" | null. "socio"/"cliente" solo si dice serlo de Aldea Global; "comerciante" = agroservicio, distribuidor o tienda.
- "cultivos": lista de {"nombre": texto, "area": número o null, "unidad": "mz" | "ha" | null}. "manzana(s)" = "mz", "hectárea(s)" = "ha".
- "municipio": texto o null. "departamento": texto o null.
- "etapa_cultivo": texto o null. "topografia": texto o null. "tipo_suelo": texto o null. "altura_msnm": número o null.
- "tipo_negocio": texto o null.
- "desconoce_terreno": true si dice que no sabe la etapa o el terreno.
- "consulta_tecnica": la pregunta agronómica o de productos que hace el usuario, copiada tal cual, o null si no pregunta nada técnico.$prompt$,
    'texto',
    'System prompt del extractor de perfil (respuesta JSON). Se envía literal.'
),
('perfil_max_preguntas', '4', 'int', 'Máximo de preguntas de perfil antes de responder con lo que haya.')
ON CONFLICT (clave) DO NOTHING;

-- El perfil entra al prompt de generación. Se AGREGA al final para no pisar
-- ediciones de producción; el placeholder {perfil} pasa a ser obligatorio.
UPDATE atv.prompt_config
   SET valor = valor || E'\n\nPERFIL DEL USUARIO:\n{perfil}',
       ts_actualizado = CURRENT_TIMESTAMP,
       actualizado_por = '003_create_perfil.sql'
 WHERE clave = 'user_prompt_template'
   AND position('{perfil}' IN valor) = 0;

UPDATE atv.prompt_config
   SET valor = valor || E'\n6. Adapta la respuesta al PERFIL DEL USUARIO (cultivo, etapa, terreno, zona, tipo de negocio). Si el perfil no alcanza para recomendar con seguridad, dilo y sugiere consultar a Asistencia Técnica. Nunca menciones datos personales.',
       ts_actualizado = CURRENT_TIMESTAMP,
       actualizado_por = '003_create_perfil.sql'
 WHERE clave = 'system_prompt'
   AND position('PERFIL DEL USUARIO' IN valor) = 0;
