-- scripts/007_regla_producto.sql
-- Corrige la regla 6 (cierre con producto, scripts/006_cierre_producto.sql)
-- tras la prueba del 2026-10-17 con el corpus v2:
--   * "¿Qué tiene para mosca blanca en tomate?" (perfil: café) cerró recomendando
--     TOTEM 60 SL, un HERBICIDA, "para el control de plagas en café": "siempre un
--     producto" pesó más que "que sirva para el problema".
--   * Tras la abstención ("No tengo esa información...") igual recomendó un producto.
-- Ahora: el producto debe tener en su ficha un uso para el MISMO problema y el
-- cultivo de la consulta (manda sobre el del perfil); si no hay, frase de la tienda;
-- y nunca después de la abstención.
--
-- Reemplaza solo la regla 6 del system_prompt y la instrucción final del
-- faq_user_prompt_template (vigentes = 006). Sin reinicio: se lee en cada consulta.
-- Idempotente (replace() no hace nada si el texto ya cambió).

UPDATE atv.prompt_config
   SET valor = replace(valor,
$viejo$6. Termina SIEMPRE recomendando un producto de AldeaZON, antes de la fuente, en una línea que empiece con "Producto AldeaZON recomendado:". Elige solo productos que aparezcan en el CONTEXTO o en PRODUCTOS DE ALDEAZON y que sirvan para el cultivo y el problema del cliente; di para qué le sirve y la dosis solo si está en la ficha. Si el diagnóstico aún no está confirmado, preséntalo como la opción para cuando lo confirme. Si ninguno de esos productos sirve para su caso, escribe "Producto AldeaZON recomendado: en su tienda AldeaZON le ayudamos a elegir el producto para su caso." Nunca recomiendes un producto que no esté en el contexto.$viejo$,
$nuevo$6. Termina recomendando un producto de AldeaZON, antes de la fuente, en una línea que empiece con "Producto AldeaZON recomendado:".
   - Elige SOLO un producto del CONTEXTO o de PRODUCTOS DE ALDEAZON cuya ficha tenga un uso para el MISMO problema de la consulta (la misma plaga, enfermedad o maleza, o el mismo fin nutricional) Y para el cultivo de la consulta. El tipo debe corresponder: nunca un herbicida para una plaga o enfermedad, ni un insecticida para una enfermedad o maleza.
   - Si la consulta nombra un cultivo, ese manda sobre el cultivo del perfil.
   - Di para qué le sirve y la dosis solo si está en la ficha. Si el diagnóstico aún no está confirmado, preséntalo como la opción para cuando lo confirme.
   - Si ningún producto cumple, escribe exactamente "Producto AldeaZON recomendado: en su tienda AldeaZON le ayudamos a elegir el producto para su caso." Es mejor eso que un producto que no corresponde.
   - Si respondiste con la frase de la regla 1 ("No tengo esa información..."), NO agregues la línea del producto.
   - Nunca recomiendes un producto que no esté en el contexto.$nuevo$),
       ts_actualizado = CURRENT_TIMESTAMP,
       actualizado_por = '007_regla_producto.sql'
 WHERE clave = 'system_prompt';

UPDATE atv.prompt_config
   SET valor = replace(valor,
'Cierre con la línea del producto AldeaZON recomendado (regla 6)',
'Cierre con la línea del producto AldeaZON recomendado (regla 6): solo un producto de PRODUCTOS DE ALDEAZON con un uso en su ficha para este mismo problema y este cultivo; si ninguno lo tiene, la frase de la tienda'),
       ts_actualizado = CURRENT_TIMESTAMP,
       actualizado_por = '007_regla_producto.sql'
 WHERE clave = 'faq_user_prompt_template';
