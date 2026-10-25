# Consola de pruebas del ATV (Streamlit)

Consola interna para que el equipo de desarrollo y Asistencia Técnica prueben el
Asistente Técnico Virtual sin pasar por la web de AldeaZON. **No es un canal para
clientes**: escucha solo en `127.0.0.1` y se usa por túnel SSH.

Archivo único: [app.py](app.py). Dependencias: [requirements.txt](requirements.txt).

## Qué tiene

### 💬 Chat

- Conversación contra la API real: `POST /api/v1/consultar` con la cabecera `X-API-Key`.
- Cada conversación lleva su `sesion_id`. La API lo devuelve en el primer mensaje
  y la consola lo reenvía en los siguientes, igual que la web. Con **Nueva
  conversación** se empieza otra sesión.
- La barra lateral permite cambiar la URL de la API, `vendedor_id` (por defecto
  `web`) y `sucursal_id`. La sucursal es opcional; si se da, la tienda del
  cliente sale primero en las existencias.
- **Mostrar detalle técnico** agrega bajo cada respuesta la etapa, la latencia,
  si se escaló a un humano (HITL), la fuente y el carrito.
- **Perfil acumulado** (barra lateral) muestra lo que el agente sabe del cliente
  en esa sesión: cultivo, zona, si es socio, etc.
- Todo queda registrado como en producción, en `atv.consulta` y `atv.sesion_perfil`.

### 📝 Formulario inicial

- Las mismas preguntas de la bienvenida del agente, en un formulario:
  - **Socio o cliente:** cédula, WhatsApp, etapa (opcional) y la consulta.
  - **No cliente:** a qué se dedica, cultivo y manzanas, municipio y comunidad,
    etapa, terreno, qué le ve a la planta, desde cuándo y qué ha aplicado.
- Al enviarlo, todo va a la API **en un solo mensaje**. Si trae lo esencial
  (quién es, cultivo, zona y problema), el agente responde directo, sin
  bienvenida ni preguntas de seguimiento.
- **Enviar como campos estructurados** (marcado por defecto): el perfil va en
  el campo `contexto` de la API y solo el problema va como texto. Así la API no
  llama al LLM que extrae el perfil: 4 llamadas en vez de 5, unos 550 tokens
  menos por conversación. Si se desmarca, todo va como texto, para comparar.
- Después se puede seguir conversando con el chat de abajo. **Nuevo formulario**
  empieza otra sesión.

### Tarjetas de consumo (Chat y Formulario)

Cada pestaña de conversación muestra el acumulado de su sesión:

| Tarjeta | Qué cuenta |
|---|---|
| Mensajes enviados | Mensajes de la conversación; en la ayuda, cuántos fueron de perfilamiento |
| Llamadas al LLM | Todas las del turno: extracción de perfil, filtro de alcance, juez de FAQ, selector de producto y respuesta |
| Tokens | Entrada + salida (el detalle está en la ayuda de la tarjeta) |
| Consumo API (USD) | gpt-4o-mini: 0,15 USD por millón de tokens de entrada y 0,60 USD por millón de salida |
| Latencia total | Suma de la latencia de cada mensaje |

### ⏱️ Latencia por paso (Chat y Formulario)

La API devuelve en cada respuesta `latencia_pasos` (`app/services/traza.py`):
cada paso del flujo con su inicio y su duración, y el total por componente
(`postgres`, `qdrant`, `embedding`, `llm`, `redis`, `cpu`).

- **Cada mensaje** tiene un desplegable con la cascada de sus pasos. Los que
  corren en paralelo (filtro de alcance y juez de FAQ; generación y selector de
  producto) se ven solapados. Debajo se indica el paso más lento.
- **La conversación** tiene un desplegable con el tiempo sumado por componente y
  los 8 pasos más lentos.

Medido el 2026-10-25 en una respuesta típica (~3–4 s): el LLM se lleva ~95 %
(la generación sola ~2 s); Postgres ~30–70 ms, Qdrant ~50–80 ms, embeddings
~140–270 ms.

### ⚙️ Prompts del agente

- Edita `atv.prompt_config` por grupo (respuesta, perfilamiento, alcance,
  preguntas frecuentes, producto e inventario).
- **Validar** y **Guardar** usan `scripts/editar_prompt_config.py` con el
  entorno de la API: la misma validación que la API (placeholders obligatorios,
  números, textos vacíos). Un valor inválido no se guarda, así la API no queda
  respondiendo 503.
- La API lee la configuración en cada mensaje: un cambio rige desde el
  siguiente, sin reiniciar.
- Cada cambio queda en `atv.prompt_config_historial` (migración `016`) con
  autor, motivo, valor anterior y nuevo. Desde el historial se ve el antes y el
  después y se puede **restaurar** el valor anterior.

La barra lateral tiene la tabla **Comparación** con las dos conversaciones lado
a lado. Los números salen del campo `consumo_llm` que la API devuelve en cada
respuesta (`app/services/consumo_llm.py`).

### 📄 Archivos del RAG

- Cruza `atv.inventario_archivo` (lo que la ingesta trajo de SeaweedFS) con el
  número de fragmentos por documento en Qdrant (colección `QDRANT_COLLECTION`).
- Indicadores: archivos en inventario, indexados, con error y fragmentos en Qdrant.
- Filtros por estado, carpeta/proveedor y texto (archivo o producto).
- Avisa si hay documentos en Qdrant que no están en el inventario (huérfanos).

### ❓ Preguntas frecuentes

- Lista `atv.pregunta_frecuente`, que se carga con
  `scripts/cargar_preguntas_frecuentes.py`.
- Columna **usos**: cuántas respuestas del agente salieron de cada pregunta, con
  la fecha del último uso.
- Filtros por cultivo, categoría, solo activas y texto.
- El detalle de una pregunta muestra la respuesta aprobada y la respuesta a evitar.

Las dos pestañas de datos se refrescan cada 60 s. El botón **Recargar datos de
las tablas** fuerza la lectura al momento.

## Seguridad

- Postgres se abre en **modo solo lectura** y Qdrant solo se lee (scroll). La
  única escritura es la pestaña **Prompts del agente**, que pasa por
  `scripts/editar_prompt_config.py` (valida y deja historial). Fuera de eso, el
  único efecto es el que tiene cualquier consulta a la API.
- Las credenciales salen del `.env` del proyecto (o de variables `ATV_*`) y
  nunca se muestran en pantalla.
- No exponga el puerto a internet. Use el túnel SSH de abajo.

## Requisitos

- La API del ATV corriendo (`aldeazon-atv.service`, `127.0.0.1:8100`):
  ```bash
  systemctl status aldeazon-atv.service
  ss -ltn | grep 8100      # debe aparecer 127.0.0.1:8100 en LISTEN
  ```
- El `.env` del proyecto con `API_KEY`, `DATABASE_URL`, `QDRANT_URL`,
  `QDRANT_API_KEY` y `QDRANT_COLLECTION`.
- Python 3.11 y un entorno virtual **aparte** del de la API (`.venv-streamlit/`,
  ignorado por git), para no mezclar dependencias.

## Instalación (una sola vez)

Desde la raíz del repo (`/var/www/aldeazon-atv`):

```bash
python3.11 -m venv .venv-streamlit
.venv-streamlit/bin/pip install -r tools/chat_streamlit/requirements.txt pandas
```

## Ejecución

```bash
cd /var/www/aldeazon-atv
.venv-streamlit/bin/streamlit run tools/chat_streamlit/app.py \
    --server.address 127.0.0.1 --server.port 8501 --server.headless true \
    --server.fileWatcherType none --client.toolbarMode minimal --server.disconnectedSessionTTL 3600
```

Para dejarla corriendo después de cerrar la terminal:

```bash
nohup .venv-streamlit/bin/streamlit run tools/chat_streamlit/app.py \
    --server.address 127.0.0.1 --server.port 8501 --server.headless true \
    --server.fileWatcherType none --client.toolbarMode minimal --server.disconnectedSessionTTL 3600 \
    > /tmp/atv_streamlit.log 2>&1 &
```

Para detenerla:

```bash
pkill -f 'streamlit run tools/chat_streamlit/app.py'
```

### Abrirla desde su computadora

El servidor escucha solo en localhost, así que se abre un túnel desde su máquina:

```bash
ssh -L 8501:127.0.0.1:8501 ostendata@<IP-del-VPS4>
```

Luego abra <http://localhost:8501> en el navegador. Desde VS Code Remote-SSH
basta con reenviar el puerto 8501 en la pestaña *Ports*.

### Conversaciones que sobreviven a una recarga

Al abrir la consola, la URL toma un identificador (`?c=…`). Cada mensaje guarda
las dos conversaciones (Chat y Formulario) en
`/tmp/atv_streamlit_conversaciones/<id>.json`: carpeta `700`, archivo `600`,
solo el usuario del servidor. Si la página se recarga con esa misma URL, las
conversaciones vuelven. Una pestaña nueva sin `?c=` empieza de cero. Los
archivos se borran al reiniciar el servidor (están en `/tmp`).

## Variables opcionales

Si no se definen, los valores salen del `.env` del proyecto.

| Variable | Para qué | Por defecto |
|---|---|---|
| `ATV_API_URL` | API contra la que conversa el chat | `http://127.0.0.1:8100` |
| `ATV_API_KEY` | API key del chat | `API_KEY` del `.env` |
| `ATV_DATABASE_URL` | Postgres para las pestañas de datos | `DATABASE_URL` del `.env` |

Ejemplo: probar contra otra instancia de la API.

```bash
ATV_API_URL=http://127.0.0.1:8200 .venv-streamlit/bin/streamlit run tools/chat_streamlit/app.py --server.address 127.0.0.1
```

## Pruebas con socios ficticios

Para probar la identificación de socio y el cierre con producto AldeaZON sin
usar datos de personas reales, hay 5 socios de prueba. Tienen ids negativos y
cédulas con el código 999, que no corresponde a ningún municipio. Se crean o se
rehacen con:

```bash
.venv/bin/python -m scripts.sembrar_perfiles_prueba            # crea o rehace los 5
.venv/bin/python -m scripts.sembrar_perfiles_prueba --borrar   # los quita
```

| Cédula | WhatsApp | Perfil | Pregunta sugerida |
|---|---|---|---|
| 999-010180-0001A | 5555-0001 | Café, Jinotega | ¿Qué tiene para la roya? |
| 999-020285-0002B | 5555-0002 | Café orgánico, El Cuá | ¿Qué le echo al café para que florezca? |
| 999-030390-0003C | 5555-0003 | Maíz y frijol, Pantasma | ¿Qué hay para gusano cogollero en el maíz? |
| 999-040495-0004D | 5555-0004 | Tomate y chiltoma, San Rafael del Norte | Tengo mosca blanca en el tomate, ¿qué le aplico? |
| 999-050575-0005E | 5555-0005 | Ganado y maíz, Wiwilí | ¿Qué semilla de pasto tiene para ganado de leche? |

Cómo seguir una prueba:

1. Pulse **Nueva conversación** y salude («Hola»). La bienvenida pide todo de una vez.
2. En **un solo mensaje** mande la cédula (con o sin guiones), el WhatsApp y la
   pregunta, p. ej.: «Soy socio, cédula 999-010180-0001A, WhatsApp 5555-0001.
   ¿Qué tiene para la roya?».
3. El asistente confirma el registro (cultivos y finca) y responde en ese mismo
   turno. La respuesta debe traer:
   - la respuesta aprobada (pregunta frecuente) o la de la ficha técnica;
   - la ampliación con la ficha técnica;
   - la línea **Producto AldeaZON recomendado**.

Las existencias por tienda y los productos con el mismo ingrediente activo
aparecen solo cuando `atv.inventario_producto` tiene datos, es decir, cuando
Semillero ya cargó `stg_insumos` y corrió `construir_inventario`.

## Problemas frecuentes

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| «Falta la API key» en la barra lateral | No hay `API_KEY` en el `.env` ni `ATV_API_KEY` | Revisar el `.env` del proyecto |
| `⚠️ Error al llamar a la API: … Connection refused` | La API está caída o reiniciando (tarda ~40 s) | `systemctl status aldeazon-atv.service` |
| `HTTP 503` | Falta una clave en `atv.prompt_config` | Aplicar las migraciones pendientes (`scripts/0NN_*.sql`) |
| `HTTP 403` «Acceso denegado» | API key incorrecta | Comparar `ATV_API_KEY` con `API_KEY` del `.env` |
| Timeout a los 60 s | Respuesta lenta del LLM o API sin memoria | Revisar `journalctl -u aldeazon-atv` y `NRestarts` |
| «No se pudo leer Qdrant» | Qdrant inaccesible o colección mal nombrada | Revisar `QDRANT_URL` y `QDRANT_COLLECTION`; la pestaña muestra igual el inventario |
| «Bad message format: Tried to use SessionInfo before it was initialized» | El navegador reconectó una sesión vieja (reinicio de la consola o cambio de `app.py` con el vigilante de archivos activo) | Cerrar la pestaña y abrir una nueva; arrancar con `--server.fileWatcherType none` (comandos de arriba) |
| La página se recarga sola o se pierde la conversación | El reenvío de puertos del IDE corta el websocket cada pocos segundos | Las conversaciones se guardan en el servidor ligadas a `?c=…` de la URL: recargue con la misma URL y vuelven. Para una conexión estable use el túnel SSH (`ssh -L`) en vez del reenvío del IDE |
| Las tablas no reflejan un cambio reciente | Caché de 60 s | **Recargar datos de las tablas** |
| El socio no se identifica | Faltan los socios de prueba o cambió `SEMILLERO_HASH_SALT` | Volver a correr `scripts.sembrar_perfiles_prueba` |
