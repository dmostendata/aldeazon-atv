-- scripts/009_producto_por_sistema.sql
-- El cierre con producto de AldeaZON deja de escribirlo el LLM de generación
-- (reglas 6 de 006/007/008: inestable con gpt-4o-mini, ver
-- app/services/producto_service.py). Ahora:
--   * producto_selector_prompt: llamada aparte (JSON {"indice": n|null}) que elige
--     la ficha del producto, en paralelo con la generación.
--   * producto_linea_template / msg_producto_tienda: la línea la arma el código
--     con los datos de esa ficha (o el mensaje de la tienda si ninguna sirve).
--   * system_prompt regla 6 y faq_user_prompt_template: el LLM ya NO escribe la
--     línea (si la escribe, el código la quita).
--
-- Despliegue: aplicar y reiniciar la API en el mismo momento (el código nuevo
-- exige las 3 claves; con el código viejo, la regla 6 nueva deja las respuestas
-- sin línea de producto hasta el reinicio). Idempotente.

INSERT INTO atv.prompt_config (clave, valor, tipo, descripcion) VALUES
(
    'producto_selector_prompt',
    $prompt$Eliges el producto de AldeaZON que se recomienda al final de la respuesta a un cliente.
Recibes la consulta, el cultivo del cliente y FICHAS numeradas (producto, cultivo, uso, dosis, condiciones).
Responde SOLO un objeto JSON: {"indice": n} con el número de la ficha elegida, o {"indice": null}.
- Elige una ficha cuyo uso sirva para el MISMO problema de la consulta (la misma plaga, enfermedad o maleza, o el mismo fin: nutrición, floración, llenado, raíces, producción de leche, etc.) y para el cultivo de la consulta. Si la consulta nombra un cultivo, ese manda sobre el del cliente; si no nombra ninguno, usa el del cliente.
- El tipo debe corresponder: nunca un herbicida para una plaga o enfermedad, ni un insecticida para una enfermedad o maleza, ni un fungicida para una plaga.
- Si la consulta pregunta por un producto concreto, elige ese producto si está en las fichas.
- Entre varias que sirven, prefiere la del cultivo exacto y la que trae dosis.
- Si ninguna sirve para ese problema y cultivo, responde {"indice": null}.$prompt$,
    'texto',
    'Selector del producto que cierra la respuesta (JSON {"indice": int|null}). Ver app/services/producto_service.py.'
),
(
    'producto_linea_template',
    'Producto AldeaZON recomendado: {producto} ({uso}).{dosis}',
    'plantilla',
    'Línea de cierre con el producto elegido. {producto} obligatorio; {uso} (objetivo en cultivo), {dosis} (" Dosis según la ficha: ..." o vacío) y {documento} permitidos.'
),
(
    'msg_producto_tienda',
    'Producto AldeaZON recomendado: en su tienda AldeaZON le ayudamos a elegir el producto para su caso.',
    'texto',
    'Línea de cierre cuando ninguna ficha recuperada sirve para el problema y cultivo de la consulta.'
)
ON CONFLICT (clave) DO NOTHING;

-- Regla 6 (es la última del system_prompt): todo desde "\n6. " hasta el final.
UPDATE atv.prompt_config
   SET valor = regexp_replace(valor, '\n6\. Termina recomendando.*$',
       E'\n6. No escribas una línea de "Producto AldeaZON recomendado": el sistema la agrega al final con un producto de las fichas. Si en la respuesta mencionas un producto, que sea solo uno del CONTEXTO o de PRODUCTOS DE ALDEAZON que sirva para el mismo problema y cultivo de la consulta.'),
       ts_actualizado = CURRENT_TIMESTAMP,
       actualizado_por = '009_producto_por_sistema.sql'
 WHERE clave = 'system_prompt' AND valor ~ '\n6\. Termina recomendando';

UPDATE atv.prompt_config
   SET valor = replace(valor,
'Cierre con la línea del producto AldeaZON recomendado (regla 6): solo un producto de PRODUCTOS DE ALDEAZON con un uso en su ficha para este mismo problema y este cultivo; si ninguno lo tiene, la frase de la tienda y luego con [Fuente: Preguntas frecuentes de Asistencia Técnica, {codigo}; ficha del producto si recomendó uno].',
'No escriba la línea del producto recomendado (la agrega el sistema). Cierre con [Fuente: Preguntas frecuentes de Asistencia Técnica, {codigo}].'),
       ts_actualizado = CURRENT_TIMESTAMP,
       actualizado_por = '009_producto_por_sistema.sql'
 WHERE clave = 'faq_user_prompt_template';
