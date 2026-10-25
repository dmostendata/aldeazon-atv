-- =====================================================================
-- PROYECTO SEMILLERO  ·  Catálogo 005 — inventario de AldeaZON para el ATV
-- PostgreSQL (semillero_control + stg_insumos)
--
-- Para qué: el Asistente Técnico Virtual (aldeazon-atv) cierra cada respuesta
-- con un producto de AldeaZON y ahora dice si hay existencias y en qué tiendas.
-- El ATV copia cada madrugada la última foto de estas tablas a
-- atv.inventario_producto (scripts/011_create_inventario.sql del repo
-- aldeazon-atv, DAG atv_perfil_productor).
--
-- Por qué tablas y no la vista stc2.vw_az_productos_inventario_ia: la vista
-- existe solo en la copia de develop (VPS1); en producción no (verificado el
-- 2026-10-24). El ATV rearma la misma lógica en su lado:
--   inventario JOIN agroquimico JOIN bodegas JOIN unidad JOIN clasificación,
--   bodega activa, producto activo, cantidad_real >= 0, sin "picadora(s)".
--
-- Primer catálogo del proyecto 'insumos' (stg_insumos). 4 tablas, módulo
-- 'inventario' (medido en producción el 2026-10-24):
--   az_bodega_inventario       16.028 filas · existencias y precios por producto y bodega
--   agroquimico                 2.363 filas · catálogo de productos
--   az_catalogo_bodegas            28 filas · bodegas / tiendas
--   agroquimico_clasificacion      11 filas · Fungicidas, Insecticidas, ...
-- La 5.ª tabla de la vista, stc2.cre_catalogo_unidad_medida (unidades), YA está
-- en el catálogo (proyecto credito, módulo catalogos -> stg_credito, diaria) y
-- un origen no puede declararse dos veces: el ATV la lee de stg_credito.
--
-- Decisiones:
--   * tipo_carga = full, diaria: tablas chicas y sin marca de cambio.
--   * campo_clave: az_bodega_inventario tiene PK (agroquimico_id,
--     az_catalogo_bodegas_id, id) pero id NO es único (16.020 distintos en
--     16.028); (agroquimico_id, az_catalogo_bodegas_id) sí (16.028 distintos).
--     agroquimico: PK compuesta en el origen, pero id es único (2.363 = 2.363).
--   * PII: az_catalogo_bodegas.colaboradores_alerta_conteo trae correos de
--     colaboradores (19 de 28 filas) -> hash_determinista. El resto son
--     catálogos, precios y cantidades.
--
-- ORDEN DE DESPLIEGUE (el de los catálogos 002-004):
--   1. Este archivo, como el dueño de semillero_control (una transacción con aserción).
--   2. Airflow -> Trigger de semillero_preparar_destino con
--        {"modo": "previsualizar", "tablas": ["az_bodega_inventario", "agroquimico",
--         "az_catalogo_bodegas", "agroquimico_clasificacion"]}
--      revisar semillero_control.vw_plan_destino_ultimo (4 CREATE TABLE, 0 errores;
--      crea stg_insumos si no existe) y repetir con "modo": "aplicar".
--   3. Despausar semillero_ingesta_insumos_inventario_01.
--   4. Permisos de lectura para el ATV (sección 4), como el dueño de
--      stg_insumos, DESPUÉS del paso 2.
--
-- Idempotente: ON CONFLICT DO NOTHING; la aserción distingue "ya estaba
-- igual" de "ya estaba, pero distinta".
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- 1. Tablas
-- ---------------------------------------------------------------------
INSERT INTO semillero_control.catalogo_tablas
  (proyecto, modulo, bd_origen, esquema_origen, tabla_origen, esquema_destino, tabla_destino,
   tipo_carga, campo_clave, frecuencia, volumen_estimado, contiene_pii, aplica_enmascaramiento,
   activo, prioridad, filtro_extraccion, responsable, notas)
VALUES
  ('insumos', 'inventario', 'stc2', 'stc2', 'az_bodega_inventario', 'stg_insumos', 'az_bodega_inventario', 'full',
   'agroquimico_id,az_catalogo_bodegas_id', 'diaria', 'medio', false, false, true, 'media', NULL, 'aldeazon-atv',
   'catálogo 005 · 16.028 filas · existencias (cantidad_real/disponible) y precios por producto y bodega; id no es único en el origen'),
  ('insumos', 'inventario', 'stc2', 'stc2', 'agroquimico', 'stg_insumos', 'agroquimico', 'full',
   'id', 'diaria', 'bajo', false, false, true, 'media', NULL, 'aldeazon-atv',
   'catálogo 005 · 2.363 filas · catálogo de productos (nombre, unidad, clasificación, estado)'),
  ('insumos', 'inventario', 'stc2', 'stc2', 'az_catalogo_bodegas', 'stg_insumos', 'az_catalogo_bodegas', 'full',
   'id', 'diaria', 'bajo', true, true, true, 'baja', NULL, 'aldeazon-atv',
   'catálogo 005 · 28 filas · bodegas/tiendas; colaboradores_alerta_conteo trae correos (hash)'),
  ('insumos', 'inventario', 'stc2', 'stc2', 'agroquimico_clasificacion', 'stg_insumos', 'agroquimico_clasificacion', 'full',
   'id', 'diaria', 'bajo', false, false, true, 'baja', NULL, 'aldeazon-atv',
   'catálogo 005 · 11 filas · clasificación del producto (Fungicidas, Insecticidas...)')
ON CONFLICT (bd_origen, esquema_origen, tabla_origen) DO NOTHING;

-- ---------------------------------------------------------------------
-- 2. Reglas de enmascaramiento
-- ---------------------------------------------------------------------
INSERT INTO semillero_control.catalogo_enmascaramiento (catalogo_id, columna, dimension, tecnica)
SELECT c.id, 'colaboradores_alerta_conteo',
       'identidad_kyc'::semillero_control.dimension_enmascaramiento_enum,
       'hash_determinista'::semillero_control.tecnica_enmascaramiento_enum
FROM semillero_control.catalogo_tablas c
WHERE c.bd_origen = 'stc2' AND c.esquema_origen = 'stc2' AND c.tabla_origen = 'az_catalogo_bodegas'
ON CONFLICT (catalogo_id, columna) DO NOTHING;

-- ---------------------------------------------------------------------
-- 3. Aserción: cada tabla como la declara este archivo y 1 regla en bodegas.
-- ---------------------------------------------------------------------
DO $$
DECLARE
    problemas text[];
    reglas_bodegas int;
BEGIN
    SELECT array_agg(format('%s: modulo=%s destino=%s.%s tipo=%s clave=%s pii=%s activo=%s',
                            e.tabla, c.modulo, c.esquema_destino, c.tabla_destino, c.tipo_carga,
                            c.campo_clave, c.contiene_pii, c.activo))
      INTO problemas
    FROM (VALUES
        ('az_bodega_inventario', 'agroquimico_id,az_catalogo_bodegas_id', false),
        ('agroquimico', 'id', false), ('az_catalogo_bodegas', 'id', true),
        ('agroquimico_clasificacion', 'id', false)
    ) AS e (tabla, clave, pii)
    LEFT JOIN semillero_control.catalogo_tablas c
      ON c.bd_origen = 'stc2' AND c.esquema_origen = 'stc2' AND c.tabla_origen = e.tabla
    WHERE c.id IS NULL
       OR c.proyecto <> 'insumos' OR c.modulo IS DISTINCT FROM 'inventario'
       OR c.esquema_destino <> 'stg_insumos' OR c.tabla_destino <> e.tabla
       OR c.tipo_carga::text <> 'full' OR c.campo_clave IS DISTINCT FROM e.clave
       OR c.contiene_pii <> e.pii OR c.aplica_enmascaramiento <> e.pii
       OR NOT c.activo OR c.filtro_extraccion IS NOT NULL;

    SELECT count(*) INTO reglas_bodegas
    FROM semillero_control.vw_reglas_enmascaramiento v
    JOIN semillero_control.catalogo_tablas c ON c.id = v.catalogo_id
    WHERE c.bd_origen = 'stc2' AND c.esquema_origen = 'stc2' AND c.tabla_origen = 'az_catalogo_bodegas';

    IF problemas IS NOT NULL OR reglas_bodegas <> 1 THEN
        RAISE EXCEPTION 'catálogo 005: configuración distinta a la declarada (reglas de az_catalogo_bodegas: %, esperado 1)', reglas_bodegas
              USING DETAIL = coalesce(array_to_string(problemas, E'\n'), '(tablas OK)'),
                    HINT   = 'Una fila o regla que ya existía no se pisa (ON CONFLICT DO NOTHING): corregirla con UPDATE y volver a correr.';
    END IF;
END $$;

COMMIT;

-- ---------------------------------------------------------------------
-- 4. Lectura para el ATV — como el dueño de stg_insumos, DESPUÉS del paso 2.
--    Solo SELECT y solo estas tablas. Rol del ATV: ostendata.
-- ---------------------------------------------------------------------
-- GRANT USAGE ON SCHEMA stg_insumos TO ostendata;
-- GRANT SELECT ON stg_insumos.az_bodega_inventario, stg_insumos.agroquimico,
--                 stg_insumos.az_catalogo_bodegas, stg_insumos.agroquimico_clasificacion
--   TO ostendata;
-- -- unidades: del catálogo de credito (como el dueño de stg_credito)
-- GRANT USAGE ON SCHEMA stg_credito TO ostendata;
-- GRANT SELECT ON stg_credito.cre_catalogo_unidad_medida TO ostendata;
