-- scripts/014_perfil_todo_de_una_vez.sql
-- Regla estricta de perfilamiento: al iniciar la conversación se pide TODO lo
-- necesario para analizar el caso en un solo mensaje, y lo que falte se pide
-- junto en un segundo mensaje, una sola vez (app/services/perfilamiento.py).
-- Antes era un dato por turno (bienvenida -> cultivo -> zona -> terreno): hasta 5
-- turnos y 5 llamadas al LLM de extracción antes de responder.
--   perfil_msg_bienvenida            socio/cliente: cédula + WhatsApp + consulta;
--                                    si no: dedicación, cultivo y área, zona,
--                                    etapa, terreno y el problema.
--   perfil_msg_pedir_faltantes       {preguntas}: una línea "- ..." por dato faltante.
--   perfil_msg_pedir_consulta        el problema con síntomas, desde cuándo y qué aplicó.
--   perfil_msg_pedir_identificacion  dijo ser socio/cliente pero no dio la cédula.
--   perfil_extraccion_prompt         consulta_tecnica incluye síntomas, tiempo y lo aplicado.
-- Aplicar y reiniciar la API juntos (el código nuevo exige las claves nuevas).
-- Idempotente.

UPDATE atv.prompt_config
   SET valor = E'¡Hola! Soy el Asistente Técnico Virtual de AldeaZON. Para analizar su caso de una sola vez, respóndame en un solo mensaje:\n\n• Si es socio o cliente de Aldea Global: su número de cédula, el número de WhatsApp que tiene registrado con nosotros y su consulta.\n• Si no lo es: a qué se dedica (productor, agroservicio, técnico), qué cultivo tiene y cuántas manzanas, en qué municipio y comunidad está, en qué etapa está el cultivo (siembra, desarrollo, floración, cosecha), cómo es el terreno (plano o quebrado, tipo de suelo, altura aproximada) y qué problema tiene: qué le ve a la planta, desde cuándo y qué ha aplicado.',
       ts_actualizado = CURRENT_TIMESTAMP, actualizado_por = '014_perfil_todo_de_una_vez.sql'
 WHERE clave = 'perfil_msg_bienvenida';

UPDATE atv.prompt_config
   SET valor = 'Gracias. Para confirmar que es usted, escríbame el número de WhatsApp que tiene registrado con Aldea Global y, si aún no me la dijo, su consulta.',
       ts_actualizado = CURRENT_TIMESTAMP, actualizado_por = '014_perfil_todo_de_una_vez.sql'
 WHERE clave = 'perfil_msg_pedir_telefono';

UPDATE atv.prompt_config
   SET valor = 'No encontré un registro con esos datos. No se preocupe, le atiendo igual.',
       ts_actualizado = CURRENT_TIMESTAMP, actualizado_por = '014_perfil_todo_de_una_vez.sql'
 WHERE clave = 'perfil_msg_no_encontrado';

UPDATE atv.prompt_config
   SET valor = replace(valor,
'- "consulta_tecnica": la pregunta agronómica o de productos que hace el usuario, copiada tal cual, o null si no pregunta nada técnico.',
'- "consulta_tecnica": la pregunta o el problema agronómico o de productos que plantea el usuario, copiado tal cual e incluyendo lo que describa del caso (síntomas, desde cuándo, qué ha aplicado), o null si no plantea nada técnico.'),
       ts_actualizado = CURRENT_TIMESTAMP, actualizado_por = '014_perfil_todo_de_una_vez.sql'
 WHERE clave = 'perfil_extraccion_prompt';

INSERT INTO atv.prompt_config (clave, valor, tipo, descripcion) VALUES
(
    'perfil_msg_pedir_faltantes',
    E'Para analizar su caso me falta lo siguiente. Respóndame todo en un solo mensaje:\n{preguntas}',
    'plantilla',
    'Lo que falta del caso, pedido junto y una sola vez. {preguntas} obligatorio (una línea "- ..." por dato, con los textos perfil_msg_pedir_*). Con un solo dato se usa directo su pregunta.'
),
(
    'perfil_msg_pedir_consulta',
    '¿Qué problema tiene en el cultivo? Cuénteme qué le ve a la planta (hojas, frutos, tallo o raíz), desde cuándo y qué ha aplicado.',
    'texto',
    'Pide la consulta técnica con lo necesario para analizar el caso.'
),
(
    'perfil_msg_pedir_identificacion',
    'Su número de cédula y el número de WhatsApp que tiene registrado con Aldea Global.',
    'texto',
    'Dijo ser socio/cliente pero no dio la cédula: se pide junto con la consulta.'
)
ON CONFLICT (clave) DO NOTHING;

-- perfil_msg_pedir_terreno ahora va como una línea más de perfil_msg_pedir_faltantes.
UPDATE atv.prompt_config
   SET valor = replace(valor, 'Para afinar la recomendación: ¿en qué etapa', '¿En qué etapa'),
       ts_actualizado = CURRENT_TIMESTAMP, actualizado_por = '014_perfil_todo_de_una_vez.sql'
 WHERE clave = 'perfil_msg_pedir_terreno' AND valor LIKE 'Para afinar la recomendación:%';
