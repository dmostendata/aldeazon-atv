-- scripts/010_selector_problema.sql
-- producto_selector_prompt (009): para "¿qué tiene para broca?" eligió KNIGHT 72 SC,
-- un FUNGICIDA (clorotalonil), porque su uso en café venía como "Coffea arabica"
-- (la extracción puso el nombre científico del cultivo como objetivo). Ahora, para
-- plagas, enfermedades y malezas, el uso de la ficha tiene que NOMBRAR el mismo
-- problema; el selector también recibe el ingrediente activo. Sin reinicio. Idempotente.
UPDATE atv.prompt_config
   SET valor = replace(valor,
'- Si la consulta pregunta por un producto concreto, elige ese producto si está en las fichas.',
'- Si la consulta es por una plaga, enfermedad o maleza, el uso de la ficha tiene que NOMBRAR ese mismo problema (o un sinónimo: "rolla" = roya, "cogollero" = Spodoptera). Un uso vacío, genérico o que solo repite el cultivo (por ejemplo "Coffea arabica") NO sirve para elegirla.
- Para nutrición o fertilización sí sirve un fertilizante o foliar del cultivo aunque su uso no nombre un problema.
- Usa el ingrediente activo para saber el tipo (por ejemplo, clorotalonil = fungicida; glifosato = herbicida).
- Si la consulta pregunta por un producto concreto, elige ese producto si está en las fichas.'),
       ts_actualizado = CURRENT_TIMESTAMP, actualizado_por = '010_selector_problema.sql'
 WHERE clave = 'producto_selector_prompt';
