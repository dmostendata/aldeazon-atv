-- =====================================================================
-- PROYECTO SEMILLERO  ·  Catálogo 004 — perfil de finca para el ATV
-- PostgreSQL 18.4  (VPS3: semillero_control + stg_cafe)
--
-- Para qué: el Asistente Técnico Virtual (aldeazon-atv) perfila a quien
-- pregunta antes de recomendar. Si es socio/cliente de Aldea, lee su finca,
-- sus lotes y sus rubros desde atv.perfil_* (scripts/003_create_perfil.sql
-- del repo aldeazon-atv), que el DAG atv_perfil_productor reconstruye cada
-- madrugada a partir de las fotos de stg_cafe.
--
-- Ya están en el catálogo y NO se tocan: ssp.persona, ssp.cliente,
-- ssp.comunidad (módulo cosecha) y ssp.asociado (módulo asociados).
--
-- 8 tablas nuevas, módulo 'perfil_finca' (proyecto cafe -> stg_cafe):
--   finca                  6.283 filas · altura, pendiente, topografía, suelo, áreas
--   finca_lote             1.835 filas · área, variedad, siembra, densidad, etapa
--   finca_lote_estado         15 filas · catálogo de etapas del lote
--   catalogo_variedad_cafe    13 filas · catálogo de variedades
--   asociado_mz_rubro     33.161 filas · rubros por asociado con manzanas (min/avg/max)
--   distrito                 209 filas · comunidad -> distrito -> municipio -> departamento
--   municipio                 44 filas
--   catalogos.ssp_departamento 10 filas (en prod NO existe ssp.departamento, solo en la
--                             copia de develop; los 44 ssp.municipio cruzan con estos ids)
--
-- Decisiones:
--   * tipo_carga = full y frecuencia diaria en las 8: son chicas, y finca /
--     finca_lote no tienen marca de cambio confiable (updatedAt solo en finca).
--   * campo_clave = PK real del origen (information_schema). asociado_mz_rubro
--     no tiene PK: (asociado_id, rubro_id) es único (33.161 = 33.161 distintos,
--     sin NULL; medido en VPS1 el 2026-10-10).
--   * Módulo nuevo -> lote nuevo semillero_ingesta_cafe_perfil_finca_01 a la
--     hora de cron_ingesta (20:00 Managua). No necesita horario propio.
--   * PII (criterio del repo: hash_determinista en identificadores, contacto,
--     texto libre y usuarios; las claves *_id nunca):
--     - finca (9 reglas): nombre (nombre de la finca, suele ser el del dueño),
--       coordenadas (varchar "lat,long" con precisión completa: no se puede
--       generalizar como número, se hashea), colindante_norte/sur/este/oeste
--       (nombres de vecinos; 2 filas traen 8 dígitos seguidos), observaciones
--       (texto libre; 2 con 8 dígitos), creadoPor y actualizadoPor (usuarios).
--       En claro: altura, porc_pendiente, topografia, tipo_suelo, áreas,
--       fechas, activo y los *_id.
--     - finca_lote: nombre del lote en claro (1.013 distintos, 0 con 8
--       dígitos: "La Loma", "Lote 1"...). Sin reglas.
--     - el resto: catálogos y cifras, sin PII.
--
-- ORDEN DE DESPLIEGUE (prod) — el mismo de los catálogos 002/003:
--   1. Este archivo, como el dueño de semillero_control:
--        psql -v ON_ERROR_STOP=1 -h <host> -U <dueño> -d <base> -f 004_cafe_perfil_finca.sql
--      Una sola transacción con aserción al final.
--   2. Airflow -> Trigger de semillero_preparar_destino con
--        {"modo": "previsualizar", "tablas": ["finca", "finca_lote", "finca_lote_estado",
--         "catalogo_variedad_cafe", "asociado_mz_rubro", "distrito", "municipio", "ssp_departamento"]}
--      revisar semillero_control.vw_plan_destino_ultimo (8 CREATE TABLE, 0 errores)
--      y repetir con "modo": "aplicar".
--   3. Despausar semillero_ingesta_cafe_perfil_finca_01.
--   4. Permisos de lectura para el ATV (sección 4 de abajo), como el dueño de
--      stg_cafe (semillero), DESPUÉS del paso 2 (las tablas tienen que existir).
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
  ('cafe', 'perfil_finca', 'ssp', 'ssp', 'finca', 'stg_cafe', 'finca', 'full', 'id', 'diaria', 'bajo', true, true, true, 'media', NULL, 'aldeazon-atv',
   'catálogo 004 · 6.283 filas · finca del asociado (asociado_persona_id = persona.id): altura, pendiente, suelo y áreas para el perfil del ATV'),
  ('cafe', 'perfil_finca', 'ssp', 'ssp', 'finca_lote', 'stg_cafe', 'finca_lote', 'full', 'id', 'diaria', 'bajo', false, false, true, 'media', NULL, 'aldeazon-atv',
   'catálogo 004 · 1.835 filas · lotes de la finca: área, variedad, siembra, densidad, etapa'),
  ('cafe', 'perfil_finca', 'ssp', 'ssp', 'finca_lote_estado', 'stg_cafe', 'finca_lote_estado', 'full', 'id', 'diaria', 'bajo', false, false, true, 'baja', NULL, 'aldeazon-atv',
   'catálogo 004 · 15 filas · etapas del lote (Siembra, Productivo Selectivo, Año de Poda...)'),
  ('cafe', 'perfil_finca', 'ssp', 'ssp', 'catalogo_variedad_cafe', 'stg_cafe', 'catalogo_variedad_cafe', 'full', 'id', 'diaria', 'bajo', false, false, true, 'baja', NULL, 'aldeazon-atv',
   'catálogo 004 · 13 filas · variedades de café (finca_lote.variedad_id)'),
  ('cafe', 'perfil_finca', 'ssp', 'ssp', 'asociado_mz_rubro', 'stg_cafe', 'asociado_mz_rubro', 'full', 'asociado_id,rubro_id', 'diaria', 'medio', false, false, true, 'media', NULL, 'aldeazon-atv',
   'catálogo 004 · 33.161 filas · rubros del asociado con manzanas min/avg/max; sin PK en el origen, (asociado_id, rubro_id) único'),
  ('cafe', 'perfil_finca', 'ssp', 'ssp', 'distrito', 'stg_cafe', 'distrito', 'full', 'id', 'diaria', 'bajo', false, false, true, 'baja', NULL, 'aldeazon-atv',
   'catálogo 004 · 209 filas · comunidad.distrito_id -> distrito.municipio_id'),
  ('cafe', 'perfil_finca', 'ssp', 'ssp', 'municipio', 'stg_cafe', 'municipio', 'full', 'id', 'diaria', 'bajo', false, false, true, 'baja', NULL, 'aldeazon-atv',
   'catálogo 004 · 44 filas · municipio.departamento_id -> departamento'),
  ('cafe', 'perfil_finca', 'catalogos', 'catalogos', 'ssp_departamento', 'stg_cafe', 'ssp_departamento', 'full', 'id', 'diaria', 'bajo', false, false, true, 'baja', NULL, 'aldeazon-atv',
   'catálogo 004 · 10 filas · departamentos (en prod no existe ssp.departamento: catalogos.ssp_departamento, los 44 ssp.municipio cruzan)')
ON CONFLICT (bd_origen, esquema_origen, tabla_origen) DO NOTHING;

-- ---------------------------------------------------------------------
-- 2. Reglas de enmascaramiento
-- ---------------------------------------------------------------------
INSERT INTO semillero_control.catalogo_enmascaramiento (catalogo_id, columna, dimension, tecnica)
SELECT c.id, r.columna, r.dimension::semillero_control.dimension_enmascaramiento_enum,
       r.tecnica::semillero_control.tecnica_enmascaramiento_enum
FROM (VALUES
  ('finca', 'nombre', 'identidad_kyc', 'hash_determinista'),          -- suele ser el nombre del dueño
  ('finca', 'coordenadas', 'geografico', 'hash_determinista'),        -- varchar "lat,long", precisión completa
  ('finca', 'colindante_norte', 'identidad_kyc', 'hash_determinista'),-- nombres de vecinos
  ('finca', 'colindante_sur', 'identidad_kyc', 'hash_determinista'),
  ('finca', 'colindante_este', 'identidad_kyc', 'hash_determinista'),
  ('finca', 'colindante_oeste', 'identidad_kyc', 'hash_determinista'),
  ('finca', 'observaciones', 'identidad_kyc', 'hash_determinista'),   -- texto libre
  ('finca', 'creadoPor', 'identidad_kyc', 'hash_determinista'),       -- usuario
  ('finca', 'actualizadoPor', 'identidad_kyc', 'hash_determinista')   -- usuario
) AS r (tabla, columna, dimension, tecnica)
JOIN semillero_control.catalogo_tablas c
  ON c.bd_origen = 'ssp' AND c.esquema_origen = 'ssp' AND c.tabla_origen = r.tabla
ON CONFLICT (catalogo_id, columna) DO NOTHING;

-- ---------------------------------------------------------------------
-- 3. Aserción: cada tabla como la declara este archivo y 9 reglas en finca.
-- ---------------------------------------------------------------------
DO $$
DECLARE
    problemas text[];
    reglas_finca int;
BEGIN
    SELECT array_agg(format('%s: modulo=%s destino=%s.%s tipo=%s clave=%s pii=%s activo=%s',
                            e.tabla, c.modulo, c.esquema_destino, c.tabla_destino, c.tipo_carga,
                            c.campo_clave, c.contiene_pii, c.activo))
      INTO problemas
    FROM (VALUES
        ('ssp', 'finca', 'id', true), ('ssp', 'finca_lote', 'id', false), ('ssp', 'finca_lote_estado', 'id', false),
        ('ssp', 'catalogo_variedad_cafe', 'id', false), ('ssp', 'asociado_mz_rubro', 'asociado_id,rubro_id', false),
        ('ssp', 'distrito', 'id', false), ('ssp', 'municipio', 'id', false),
        ('catalogos', 'ssp_departamento', 'id', false)
    ) AS e (bd, tabla, clave, pii)
    LEFT JOIN semillero_control.catalogo_tablas c
      ON c.bd_origen = e.bd AND c.esquema_origen = e.bd AND c.tabla_origen = e.tabla
    WHERE c.id IS NULL
       OR c.proyecto <> 'cafe' OR c.modulo IS DISTINCT FROM 'perfil_finca'
       OR c.esquema_destino <> 'stg_cafe' OR c.tabla_destino <> e.tabla
       OR c.tipo_carga::text <> 'full' OR c.campo_clave IS DISTINCT FROM e.clave
       OR c.contiene_pii <> e.pii OR c.aplica_enmascaramiento <> e.pii
       OR NOT c.activo OR c.filtro_extraccion IS NOT NULL;

    SELECT count(*) INTO reglas_finca
    FROM semillero_control.vw_reglas_enmascaramiento v
    JOIN semillero_control.catalogo_tablas c ON c.id = v.catalogo_id
    WHERE c.bd_origen = 'ssp' AND c.esquema_origen = 'ssp' AND c.tabla_origen = 'finca';

    IF problemas IS NOT NULL OR reglas_finca <> 9 THEN
        RAISE EXCEPTION 'catálogo 004: configuración distinta a la declarada (reglas de finca: %, esperado 9)', reglas_finca
              USING DETAIL = coalesce(array_to_string(problemas, E'\n'), '(tablas OK)'),
                    HINT   = 'Una fila o regla que ya existía no se pisa (ON CONFLICT DO NOTHING): corregirla con UPDATE y volver a correr.';
    END IF;
END $$;

COMMIT;

-- ---------------------------------------------------------------------
-- 4. Lectura para el ATV — como el dueño de stg_cafe, DESPUÉS del paso 2
--    del ORDEN DE DESPLIEGUE. Solo SELECT y solo las tablas que lee
--    scripts/003_create_perfil.sql (aldeazon-atv). El rol es el de
--    DATABASE_URL del ATV (hoy: ostendata).
-- ---------------------------------------------------------------------
-- GRANT USAGE ON SCHEMA stg_cafe TO ostendata;
-- GRANT SELECT ON stg_cafe.persona, stg_cafe.asociado, stg_cafe.cliente, stg_cafe.comunidad,
--                 stg_cafe.finca, stg_cafe.finca_lote, stg_cafe.finca_lote_estado,
--                 stg_cafe.catalogo_variedad_cafe, stg_cafe.asociado_mz_rubro,
--                 stg_cafe.distrito, stg_cafe.municipio, stg_cafe.ssp_departamento
--   TO ostendata;

-- ---------------------------------------------------------------------
-- Verificación
-- ---------------------------------------------------------------------
-- SELECT tabla_origen, activo, contiene_pii,
--        (SELECT count(*) FROM semillero_control.vw_reglas_enmascaramiento v WHERE v.catalogo_id = c.id) AS reglas
-- FROM semillero_control.catalogo_tablas c
-- WHERE proyecto = 'cafe' AND modulo = 'perfil_finca' ORDER BY id;
-- -- 8 filas; 9 reglas en finca, 0 en el resto
