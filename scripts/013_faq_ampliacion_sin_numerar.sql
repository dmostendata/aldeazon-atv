-- scripts/013_faq_ampliacion_sin_numerar.sql
-- Ajuste de 012 (faq_user_prompt_template), medido el 2026-10-24:
--   * el LLM escribía literalmente "(1)" y "(2)";
--   * cuando ninguna ficha servía escribía "No tengo información en las fichas...",
--     frase de abstención que descarta toda la respuesta (REGEX_ABSTENCION) y deja la
--     respuesta aprobada literal. Ahora omite la ampliación sin mencionarla.
-- Sin reinicio. Idempotente (replace()).
UPDATE atv.prompt_config
   SET valor = replace(replace(valor,
'Responda al cliente en dos partes: (1) la respuesta aprobada, ordenada y adaptada a su perfil, sin contradecirla; (2) amplíe la solución',
'Responda al cliente en dos párrafos, sin numerarlos: primero la respuesta aprobada, ordenada y adaptada a su perfil, sin contradecirla; después amplíe la solución'),
'Si ninguna ficha corresponde al problema y cultivo, no la use.',
'Si ninguna ficha corresponde al problema y cultivo, escriba solo el primer párrafo y no mencione las fichas (no diga que no tiene información).'),
       ts_actualizado = CURRENT_TIMESTAMP, actualizado_por = '013_faq_ampliacion_sin_numerar.sql'
 WHERE clave = 'faq_user_prompt_template';
