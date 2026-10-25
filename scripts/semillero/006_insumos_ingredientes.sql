-- =====================================================================
-- PROYECTO SEMILLERO  ·  Catálogo 006 — ingredientes activos de los productos
-- PostgreSQL (semillero_control + stg_insumos)
--
-- Para qué: el ATV toma el ingrediente activo de la ficha técnica del producto
-- que recomienda (p. ej. "Fluoxastrobin + Tebuconazole") y busca en la tabla de
-- productos de AldeaZON todos los que lo contienen, con sus existencias
-- (atv.inventario_producto, scripts/construir_inventario.py del repo aldeazon-atv).
--
-- 2 tablas más del módulo 'inventario' (proyecto insumos, catálogo 005); se
-- cargan en el mismo lote semillero_ingesta_insumos_inventario_01. Medido en
-- producción el 2026-10-24:
--   ingrediente_activo               257 filas · nombre del ingrediente (Tebuconazole, Mancozeb...)
--   agroquimico_ingrediente_activo 1.666 filas · producto <-> ingrediente (747 productos)
--
-- Decisiones: full, diaria; campo_clave = id (PK del origen, único en ambas).
-- Sin PII. No estaban en el catálogo de ningún proyecto.
--
-- ORDEN DE DESPLIEGUE: igual que el 005 (este archivo como dueño de
-- semillero_control -> semillero_preparar_destino previsualizar/aplicar con
-- {"tablas": ["ingrediente_activo", "agroquimico_ingrediente_activo"]} ->
-- GRANT de la sección 3). Idempotente.
-- =====================================================================

BEGIN;

INSERT INTO semillero_control.catalogo_tablas
  (proyecto, modulo, bd_origen, esquema_origen, tabla_origen, esquema_destino, tabla_destino,
   tipo_carga, campo_clave, frecuencia, volumen_estimado, contiene_pii, aplica_enmascaramiento,
   activo, prioridad, filtro_extraccion, responsable, notas)
VALUES
  ('insumos', 'inventario', 'stc2', 'stc2', 'ingrediente_activo', 'stg_insumos', 'ingrediente_activo', 'full',
   'id', 'diaria', 'bajo', false, false, true, 'baja', NULL, 'aldeazon-atv',
   'catálogo 006 · 257 filas · ingredientes activos (nombre, tipo, estado)'),
  ('insumos', 'inventario', 'stc2', 'stc2', 'agroquimico_ingrediente_activo', 'stg_insumos', 'agroquimico_ingrediente_activo', 'full',
   'id', 'diaria', 'bajo', false, false, true, 'baja', NULL, 'aldeazon-atv',
   'catálogo 006 · 1.666 filas · producto (agroquimico_id) <-> ingrediente activo (747 productos)')
ON CONFLICT (bd_origen, esquema_origen, tabla_origen) DO NOTHING;

-- Aserción: las 2 tablas como las declara este archivo.
DO $$
DECLARE
    problemas text[];
BEGIN
    SELECT array_agg(format('%s: proyecto=%s modulo=%s destino=%s.%s tipo=%s clave=%s pii=%s activo=%s',
                            e.tabla, c.proyecto, c.modulo, c.esquema_destino, c.tabla_destino, c.tipo_carga,
                            c.campo_clave, c.contiene_pii, c.activo))
      INTO problemas
    FROM (VALUES ('ingrediente_activo'), ('agroquimico_ingrediente_activo')) AS e (tabla)
    LEFT JOIN semillero_control.catalogo_tablas c
      ON c.bd_origen = 'stc2' AND c.esquema_origen = 'stc2' AND c.tabla_origen = e.tabla
    WHERE c.id IS NULL
       OR c.proyecto <> 'insumos' OR c.modulo IS DISTINCT FROM 'inventario'
       OR c.esquema_destino <> 'stg_insumos' OR c.tabla_destino <> e.tabla
       OR c.tipo_carga::text <> 'full' OR c.campo_clave IS DISTINCT FROM 'id'
       OR c.contiene_pii OR c.aplica_enmascaramiento OR NOT c.activo OR c.filtro_extraccion IS NOT NULL;

    IF problemas IS NOT NULL THEN
        RAISE EXCEPTION 'catálogo 006: configuración distinta a la declarada'
              USING DETAIL = array_to_string(problemas, E'\n'),
                    HINT   = 'Una fila que ya existía no se pisa (ON CONFLICT DO NOTHING): corregirla con UPDATE y volver a correr.';
    END IF;
END $$;

COMMIT;

-- ---------------------------------------------------------------------
-- 3. Lectura para el ATV — como el dueño de stg_insumos, después de preparar_destino.
-- ---------------------------------------------------------------------
-- GRANT SELECT ON stg_insumos.ingrediente_activo, stg_insumos.agroquimico_ingrediente_activo TO ostendata;
