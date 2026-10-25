-- scripts/012_ingredientes_y_ficha.sql
-- 1. atv.inventario_producto + ingredientes activos de cada producto
--    (stg_insumos.ingrediente_activo / agroquimico_ingrediente_activo, catálogo
--    Semillero 006). El cierre de la respuesta busca el ingrediente activo de la
--    ficha del producto recomendado en la tabla de productos y lista los que lo
--    contienen, con sus existencias (app/services/producto_service.py).
--      ingredientes        nombres tal como están en Aldea ("Tebuconazole")
--      ingredientes_clave  mismo orden, palabras normalizadas ("tebuconasol"),
--                          para cruzar con la ficha aunque una diga
--                          "Chlorothalonil" y la otra "Clorotalonil".
-- 2. faq_user_prompt_template: la respuesta de una pregunta frecuente SIEMPRE se
--    amplía con las fichas técnicas que correspondan al mismo problema y cultivo.
-- 3. inventario_alternativas_template, inventario_max_productos.
-- Idempotente. Aplicar y reiniciar la API juntos (el código nuevo exige las claves
-- nuevas; el viejo no conoce las columnas, pero no las necesita).

ALTER TABLE atv.inventario_producto
    ADD COLUMN IF NOT EXISTS ingredientes       TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS ingredientes_clave TEXT[] NOT NULL DEFAULT '{}';

UPDATE atv.prompt_config
   SET valor = E'CONTEXTO 1 - respuesta aprobada por Asistencia Técnica de Aldea Global ({codigo}):\nPregunta frecuente: {pregunta}\nRespuesta aprobada: {respuesta}\nLo que NUNCA debe hacer al responder: {evitar}\n\nCONTEXTO 2 - fichas técnicas de productos de AldeaZON relacionadas con la consulta:\n{productos}\n\nCONSULTA DEL CLIENTE:\n{consulta}\n\nPERFIL DEL USUARIO:\n{perfil}\n\nResponda al cliente en dos partes: (1) la respuesta aprobada, ordenada y adaptada a su perfil, sin contradecirla; (2) amplíe la solución con lo que digan las fichas técnicas del CONTEXTO 2 que correspondan al mismo problema y cultivo (qué producto, cómo y cuándo aplicarlo, dosis, intervalo, precauciones), copiando cifras y unidades tal como están en la ficha. Si ninguna ficha corresponde al problema y cultivo, no la use. No escriba la línea del producto recomendado (la agrega el sistema). Cierre con [Fuente: Preguntas frecuentes de Asistencia Técnica, {codigo}; y la ficha técnica que haya usado].',
       ts_actualizado = CURRENT_TIMESTAMP,
       actualizado_por = '012_ingredientes_y_ficha.sql'
 WHERE clave = 'faq_user_prompt_template';

INSERT INTO atv.prompt_config (clave, valor, tipo, descripcion) VALUES
(
    'inventario_alternativas_template',
    E'Otros productos AldeaZON con el mismo ingrediente activo ({ingredientes}), con existencias (revise su ficha y dosis antes de usarlos):\n{productos}',
    'plantilla',
    'Productos de la tabla de productos de AldeaZON que contienen el ingrediente activo de la ficha recomendada. {productos} obligatorio (una línea por producto con sus tiendas); {ingredientes} permitido.'
),
(
    'inventario_max_productos',
    '3',
    'int',
    'Cuántos productos con el mismo ingrediente activo se listan (los de más existencias). 0 no los lista.'
)
ON CONFLICT (clave) DO NOTHING;
