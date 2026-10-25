-- scripts/011_create_inventario.sql
-- Existencias de AldeaZON por producto y tienda, para cerrar cada respuesta con
-- el producto recomendado, si hay inventario y en qué tiendas.
--
--   atv.inventario_producto  Copia de la última foto de stg_insumos (Semillero,
--                            catálogo scripts/semillero/005_insumos_inventario.sql)
--                            con la lógica de la vista stc2.vw_az_productos_inventario_ia
--                            (que solo existe en develop). La reconstruye cada
--                            madrugada scripts/construir_inventario.py (DAG
--                            atv_perfil_productor). La API solo lee.
--                            nombre_clave = nombre sin cifras ni siglas de
--                            formulación ("Evito T 450 SC" -> "evitot"), para
--                            cruzar con el producto de la ficha técnica.
--   atv.prompt_config        inventario_template, msg_sin_existencias,
--                            inventario_max_tiendas.
--
-- Idempotente. Sin reinicio para la tabla; las claves las exige el código nuevo.

CREATE TABLE IF NOT EXISTS atv.inventario_producto (
    producto_id          INTEGER      NOT NULL,
    bodega_id            INTEGER      NOT NULL,
    producto_nombre      VARCHAR(255) NOT NULL,
    nombre_clave         VARCHAR(255) NOT NULL,
    bodega_nombre        VARCHAR(120) NOT NULL,
    sucursal_id          INTEGER,
    unidad               VARCHAR(80),
    clasificacion        VARCHAR(120),
    cantidad_disponible  NUMERIC(14, 2) NOT NULL,
    cantidad_real        NUMERIC(14, 2),
    fecha_foto           DATE         NOT NULL,
    ts_construido        TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (producto_id, bodega_id)
);
CREATE INDEX IF NOT EXISTS idx_inventario_producto_clave ON atv.inventario_producto (nombre_clave);

INSERT INTO atv.prompt_config (clave, valor, tipo, descripcion) VALUES
(
    'inventario_template',
    'Disponible en: {tiendas}.',
    'plantilla',
    'Existencias del producto recomendado, tras la línea del producto. {tiendas} obligatorio ("Pantasma: 23 Litros; San Rafael: 12 Litros y 3 tiendas más").'
),
(
    'msg_sin_existencias',
    'Por ahora no tenemos existencias en tiendas; consulte en su tienda AldeaZON cuándo llega o una alternativa.',
    'texto',
    'Cuando el producto recomendado no tiene existencias en ninguna tienda.'
),
(
    'inventario_max_tiendas',
    '3',
    'int',
    'Cuántas tiendas se nombran (primero la sucursal del cliente, luego las de más existencias). 0 no muestra inventario.'
)
ON CONFLICT (clave) DO NOTHING;
