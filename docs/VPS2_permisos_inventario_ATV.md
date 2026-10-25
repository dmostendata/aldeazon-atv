# Permisos de lectura del ATV sobre el inventario (Semillero)

**Para:** administrador de la base `semillero`
**Tiempo estimado:** 2 minutos
**Riesgo:** bajo. Son **2 líneas** de permiso de uso de esquema para un solo rol. No se crean, modifican ni borran datos.

## Resumen: lo único que falta

Ejecutar como **superusuario** o como **`yader`** (dueño de los esquemas), en la base `semillero` de `10.0.0.14:5432`:

```sql
GRANT USAGE ON SCHEMA stg_insumos TO ostendata;
GRANT USAGE ON SCHEMA stg_credito TO ostendata;
```

Los permisos de lectura sobre las 7 tablas (`GRANT SELECT`) **ya están dados**. Se dieron el 2026-10-24 con un DAG temporal que corrió como `semillero`, dueño de las tablas, y luego se borró. El usuario `semillero` no puede dar el USAGE porque los esquemas son de `yader`.

## Para qué

El Asistente Técnico Virtual (ATV) cierra cada respuesta con el producto AldeaZON que recomienda. Con estos permisos podrá decir además **si hay existencias y en qué tiendas**, primero la del cliente, y mostrar los otros productos que tienen el mismo ingrediente activo.

Los datos ya están cargados en Semillero. Lo único que falta es que el rol del ATV (`ostendata`) pueda leerlos. Hoy recibe `permission denied for schema stg_insumos`.

## Estado al 2026-10-24

| Paso | Estado |
|---|---|
| Tablas registradas en `semillero_control.catalogo_tablas` (catálogos 005 y 006) | ✅ Hecho |
| Tablas destino creadas (`semillero_preparar_destino`, modo aplicar) | ✅ Hecho, 6 de 6 en estado `aplicado`, sin errores |
| DAG `semillero_ingesta_insumos_inventario_01` | ✅ Activo; primera carga exitosa (detalle abajo) |
| DAG `atv_perfil_productor` con la tarea `construir_inventario` | ✅ Publicado, sin errores de importación |
| `GRANT SELECT` sobre las 7 tablas para `ostendata` | ✅ Hecho (DAG temporal, ya borrado) |
| **`GRANT USAGE` de los esquemas `stg_insumos` y `stg_credito`** | ❌ **Pendiente: este documento** |

Primera carga, foto `2026-10-24`:

| Tabla | Filas |
|---|---|
| `stg_insumos.az_bodega_inventario` | 16.028 |
| `stg_insumos.agroquimico` | 2.363 |
| `stg_insumos.az_catalogo_bodegas` | 28 |
| `stg_insumos.agroquimico_clasificacion` | 11 |
| `stg_insumos.ingrediente_activo` | 257 |
| `stg_insumos.agroquimico_ingrediente_activo` | 1.666 |

## Dónde se ejecuta

- **Servidor de base de datos:** `10.0.0.14:5432`, base `semillero`. Se puede ejecutar desde VPS2 o desde cualquier máquina con acceso a ese servidor.
- **Usuario:** un **superusuario**. Si no hay uno disponible, se reparte así:

  | Objeto | Dueño | Qué ejecuta |
  |---|---|---|
  | esquemas `stg_insumos` y `stg_credito` | `yader` | los dos `GRANT USAGE ON SCHEMA …` |
  | las 7 tablas | `semillero` | los `GRANT SELECT ON …` |

## Pasos

### 1. Ejecutar el script

Basta con las 2 líneas del resumen. El script completo también sirve, y repetir los SELECT no cambia nada. El script está en el repositorio `aldeazon-atv`: [`scripts/semillero/007_grants_atv_inventario.sql`](../scripts/semillero/007_grants_atv_inventario.sql).

```bash
psql "host=10.0.0.14 port=5432 dbname=semillero user=<SUPERUSUARIO>" \
     -v ON_ERROR_STOP=1 -f 007_grants_atv_inventario.sql
```

<details>
<summary>Contenido del script, por si se prefiere pegar</summary>

```sql
BEGIN;

GRANT USAGE ON SCHEMA stg_insumos TO ostendata;
GRANT SELECT ON stg_insumos.az_bodega_inventario,
                stg_insumos.agroquimico,
                stg_insumos.az_catalogo_bodegas,
                stg_insumos.agroquimico_clasificacion,
                stg_insumos.ingrediente_activo,
                stg_insumos.agroquimico_ingrediente_activo
  TO ostendata;

GRANT USAGE ON SCHEMA stg_credito TO ostendata;
GRANT SELECT ON stg_credito.cre_catalogo_unidad_medida TO ostendata;

COMMIT;
```

</details>

Notas:
- Es idempotente: se puede correr más de una vez sin problema.
- Las tablas están particionadas por `_fecha_carga`. El permiso sobre la tabla padre basta y cubre también las particiones de los meses siguientes, así que **no hace falta repetirlo cada mes**.
- `az_catalogo_bodegas.colaboradores_alerta_conteo` (correos) ya viene enmascarada con hash desde la ingesta.

### 2. Verificar

El mismo script termina con una consulta de verificación. Las **9 filas deben decir `true`**:

```
          objeto                                 | ok
-------------------------------------------------+----
 schema stg_insumos                              | t
 schema stg_credito                              | t
 tabla stg_insumos.az_bodega_inventario          | t
 tabla stg_insumos.agroquimico                   | t
 tabla stg_insumos.az_catalogo_bodegas           | t
 tabla stg_insumos.agroquimico_clasificacion     | t
 tabla stg_insumos.ingrediente_activo            | t
 tabla stg_insumos.agroquimico_ingrediente_activo| t
 tabla stg_credito.cre_catalogo_unidad_medida    | t
```

### 3. Avisar al equipo del ATV

Con eso basta. El equipo del ATV construye el inventario del agente y lo verifica. A partir de ahí se actualiza solo cada madrugada con el DAG `atv_perfil_productor`.

Si el administrador prefiere dejarlo listo de una vez, desde la UI de Airflow puede ejecutar `atv_perfil_productor` y revisar que la tarea `construir_inventario` termine en `success`.

## Si algo falla

| Error | Causa | Qué hacer |
|---|---|---|
| `must be owner of schema stg_insumos` | El usuario no es dueño del esquema (es `yader`) | Ejecutar como superusuario o como `yader` |
| `permission denied for table …` al hacer el GRANT | El usuario no es dueño de la tabla (es `semillero`) | Ejecutar como superusuario o como `semillero` |
| `role "ostendata" does not exist` | El rol del ATV no existe en este servidor | No debería pasar (se verificó que existe). Avisar al equipo del ATV |
| `relation … does not exist` | Se ejecutó en otra base | Confirmar `dbname=semillero` en `10.0.0.14` |

## Cómo revertir

Si hiciera falta quitar el acceso:

```sql
REVOKE SELECT ON stg_insumos.az_bodega_inventario, stg_insumos.agroquimico,
                 stg_insumos.az_catalogo_bodegas, stg_insumos.agroquimico_clasificacion,
                 stg_insumos.ingrediente_activo, stg_insumos.agroquimico_ingrediente_activo,
                 stg_credito.cre_catalogo_unidad_medida
  FROM ostendata;
REVOKE USAGE ON SCHEMA stg_insumos FROM ostendata;
-- stg_credito: revocar USAGE solo si ostendata no lee otras tablas de ese esquema
```

El ATV sigue funcionando sin el inventario. En ese caso, en vez de las existencias dice: «En este momento no pude consultar las existencias en tiendas; confírmelas en su tienda AldeaZON».
