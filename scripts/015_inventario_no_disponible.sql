-- scripts/015_inventario_no_disponible.sql
-- Cuando atv.inventario_producto aún no está cargado (o falla la consulta), el
-- cierre lo dice en vez de omitir las existencias (producto_service.texto_existencias).
-- Aplicar y reiniciar la API juntos (el código nuevo exige la clave). Idempotente.
INSERT INTO atv.prompt_config (clave, valor, tipo, descripcion) VALUES
(
    'msg_inventario_no_disponible',
    'En este momento no pude consultar las existencias en tiendas; confírmelas en su tienda AldeaZON.',
    'texto',
    'Tras la línea del producto, cuando el inventario no está cargado o no se pudo consultar. No afirma que no haya existencias.'
)
ON CONFLICT (clave) DO NOTHING;
