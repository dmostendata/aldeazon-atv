-- =====================================================================
-- PROYECTO SEMILLERO  ·  007 — permisos de lectura del ATV sobre el inventario
-- PostgreSQL, base `semillero` (10.0.0.14:5432)
--
-- Ejecutar como SUPERUSUARIO (o como yader para los USAGE y como semillero
-- para los SELECT: los esquemas son de yader y las tablas de semillero).
--
-- 2026-10-24: los GRANT SELECT ya se dieron (DAG temporal como `semillero`,
-- ya borrado). Solo falta el USAGE de los dos esquemas (dueño: yader). Correr
-- el archivo completo igual es seguro: los SELECT repetidos no cambian nada.
--
-- Junta las secciones de permisos de los catálogos 005 y 006. Solo lectura
-- (USAGE + SELECT), solo estas 7 tablas, rol ostendata (el ATV en VPS4).
-- Las tablas son particionadas por _fecha_carga: el permiso sobre la tabla
-- padre basta y cubre también las particiones de los meses siguientes.
-- Idempotente: se puede correr más de una vez.
-- =====================================================================

BEGIN;

GRANT USAGE ON SCHEMA stg_insumos TO ostendata;
GRANT SELECT ON stg_insumos.az_bodega_inventario,
                stg_insumos.agroquimico,
                stg_insumos.az_catalogo_bodegas,
                stg_insumos.agroquimico_clasificacion,
                stg_insumos.ingrediente_activo,
                stg_insumos.agroquimico_ingrediente_activo
  TO ostendata;

-- unidades de medida: ya las carga el catálogo de crédito
GRANT USAGE ON SCHEMA stg_credito TO ostendata;
GRANT SELECT ON stg_credito.cre_catalogo_unidad_medida TO ostendata;

COMMIT;

-- Verificación: las 9 filas deben decir true.
SELECT 'schema '  || s AS objeto, has_schema_privilege('ostendata', s, 'USAGE') AS ok
  FROM unnest(ARRAY['stg_insumos', 'stg_credito']) AS s
UNION ALL
SELECT 'tabla '   || t, has_table_privilege('ostendata', t, 'SELECT')
  FROM unnest(ARRAY['stg_insumos.az_bodega_inventario', 'stg_insumos.agroquimico',
                    'stg_insumos.az_catalogo_bodegas', 'stg_insumos.agroquimico_clasificacion',
                    'stg_insumos.ingrediente_activo', 'stg_insumos.agroquimico_ingrediente_activo',
                    'stg_credito.cre_catalogo_unidad_medida']) AS t;
