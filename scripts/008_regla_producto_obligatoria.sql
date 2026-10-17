-- scripts/008_regla_producto_obligatoria.sql
-- Ajuste de la regla 6 (scripts/007_regla_producto.sql). Con 007, gpt-4o-mini
-- quedó demasiado conservador (2026-10-17, corpus v2): con TWISTER 40 SE y
-- CONTACT 11.25 SE (tomate, mosca blanca) en el contexto cerró con la frase de la
-- tienda; igual con la semilla Mombaza (objetivo: leche), y nombró DESTROYER en el
-- texto pero no en la línea final. Ahora: si hay un producto que coincide, DEBE
-- recomendarlo (y es el mismo que nombró en el texto); la frase de la tienda solo
-- si ninguno coincide. Sin reinicio. Idempotente (replace()).

UPDATE atv.prompt_config
   SET valor = replace(replace(valor,
$viejo1$   - Elige SOLO un producto del CONTEXTO o de PRODUCTOS DE ALDEAZON cuya ficha tenga un uso para el MISMO problema de la consulta (la misma plaga, enfermedad o maleza, o el mismo fin nutricional) Y para el cultivo de la consulta. El tipo debe corresponder: nunca un herbicida para una plaga o enfermedad, ni un insecticida para una enfermedad o maleza.$viejo1$,
$nuevo1$   - Revisa los productos del CONTEXTO y de PRODUCTOS DE ALDEAZON. Un producto COINCIDE si su ficha tiene un uso para el mismo problema de la consulta (la misma plaga, enfermedad o maleza, o el mismo fin: nutrición, floración, producción de leche, etc.) y para el cultivo de la consulta. El tipo debe corresponder: nunca un herbicida para una plaga o enfermedad, ni un insecticida para una enfermedad o maleza.
   - Si al menos un producto COINCIDE, DEBES recomendarlo en esa línea (el que mejor coincida; si ya lo nombraste en la respuesta, es ese mismo).$nuevo1$),
$viejo2$   - Si ningún producto cumple, escribe exactamente$viejo2$,
$nuevo2$   - Solo si NINGUNO coincide, escribe exactamente$nuevo2$),
       ts_actualizado = CURRENT_TIMESTAMP,
       actualizado_por = '008_regla_producto_obligatoria.sql'
 WHERE clave = 'system_prompt';
