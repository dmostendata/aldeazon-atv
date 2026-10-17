# tools/chat_streamlit/app.py
"""
Consola de pruebas del ATV (equipo de desarrollo, no es la web de AldeaZON).

    .venv-streamlit/bin/streamlit run tools/chat_streamlit/app.py --server.address 127.0.0.1

Pestañas:
  - Chat: POST /api/v1/consultar, con un sesion_id propio por conversación.
  - Archivos del RAG: atv.inventario_archivo (lo que la ingesta trajo de SeaweedFS)
    + fragmentos por documento en Qdrant.
  - Preguntas frecuentes: atv.pregunta_frecuente + cuántas veces las usó el agente.

Las credenciales salen de ATV_* o del .env del proyecto y nunca se muestran.
Postgres se abre en modo solo lectura y Qdrant solo se lee (scroll).
"""
import os
import re
from pathlib import Path

import pandas as pd
import psycopg
import requests
import streamlit as st
from dotenv import dotenv_values

RAIZ = Path(__file__).resolve().parents[2]
ENV = dotenv_values(RAIZ / ".env")
URL_POR_DEFECTO = os.getenv("ATV_API_URL", "http://127.0.0.1:8100")
API_KEY = os.getenv("ATV_API_KEY") or ENV.get("API_KEY")
# La app usa postgresql+psycopg:// (SQLAlchemy); psycopg quiere postgresql://
DATABASE_URL = re.sub(r"^postgresql\+\w+://", "postgresql://", os.getenv("ATV_DATABASE_URL") or ENV.get("DATABASE_URL") or "")
QDRANT_URL = (ENV.get("QDRANT_URL") or "").rstrip("/")
QDRANT_API_KEY = ENV.get("QDRANT_API_KEY")
QDRANT_COLLECTION = ENV.get("QDRANT_COLLECTION")
TIMEOUT_S = 60
CACHE_TTL_S = 60

st.set_page_config(page_title="ATV · Consola de pruebas", page_icon="🌱", layout="wide")


# --- Datos (solo lectura) ---------------------------------------------------
def leer_sql(sql: str) -> pd.DataFrame:
    with psycopg.connect(DATABASE_URL, connect_timeout=10) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute(sql)
            return pd.DataFrame(cur.fetchall(), columns=[c.name for c in cur.description])


@st.cache_data(ttl=CACHE_TTL_S, show_spinner="Leyendo atv.inventario_archivo…")
def cargar_inventario() -> pd.DataFrame:
    return leer_sql("""
        SELECT nombre_archivo, estado, metodo_extraccion, total_registros_atomicos,
               version_corpus, ts_cargado, ts_indexado
          FROM atv.inventario_archivo
         ORDER BY nombre_archivo""")


@st.cache_data(ttl=CACHE_TTL_S, show_spinner="Contando fragmentos en Qdrant…")
def cargar_fragmentos_qdrant() -> pd.DataFrame:
    """Fragmentos por documento_fuente (scroll paginado, solo ese campo del payload)."""
    conteo, offset = {}, None
    headers = {"api-key": QDRANT_API_KEY} if QDRANT_API_KEY else {}
    while True:
        cuerpo = {"limit": 500, "with_payload": ["documento_fuente", "producto"], "with_vector": False}
        if offset is not None:
            cuerpo["offset"] = offset
        r = requests.post(f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points/scroll",
                          json=cuerpo, headers=headers, timeout=30)
        r.raise_for_status()
        resultado = r.json()["result"]
        for punto in resultado["points"]:
            p = punto.get("payload") or {}
            doc = p.get("documento_fuente")
            fila = conteo.setdefault(doc, {"documento": doc, "fragmentos": 0, "productos": set()})
            fila["fragmentos"] += 1
            if p.get("producto"):
                fila["productos"].add(str(p["producto"]))
        offset = resultado.get("next_page_offset")
        if offset is None:
            break
    filas = [{**f, "productos": ", ".join(sorted(f["productos"]))} for f in conteo.values()]
    return pd.DataFrame(filas, columns=["documento", "fragmentos", "productos"])


@st.cache_data(ttl=CACHE_TTL_S, show_spinner="Leyendo atv.pregunta_frecuente…")
def cargar_preguntas() -> pd.DataFrame:
    return leer_sql("""
        SELECT f.codigo, f.cultivo, f.categoria, f.pregunta,
               array_to_string(f.variantes, ' | ') AS variantes,
               f.respuesta, f.respuesta_evitar, f.activo,
               coalesce(u.usos, 0) AS usos, u.ultimo_uso,
               f.archivo_origen, f.ts_actualizado
          FROM atv.pregunta_frecuente f
          LEFT JOIN (
                SELECT registros_usados->0->>'codigo' AS codigo, count(*) AS usos, max(ts) AS ultimo_uso
                  FROM atv.consulta
                 WHERE modelo LIKE '%faq%'
                 GROUP BY 1
          ) u ON u.codigo = f.codigo
         ORDER BY f.codigo""")


# --- Barra lateral ------------------------------------------------------------
def nueva_conversacion():
    st.session_state.mensajes = []
    st.session_state.sesion_id = None
    st.session_state.perfil = None


if "mensajes" not in st.session_state:
    nueva_conversacion()

with st.sidebar:
    st.header("Chat")
    url = st.text_input("URL de la API", URL_POR_DEFECTO)
    vendedor_id = st.text_input("vendedor_id", "web")
    sucursal = st.text_input("sucursal_id (opcional)", "")
    mostrar_detalle = st.checkbox("Mostrar detalle técnico", value=True)
    st.button("Nueva conversación", on_click=nueva_conversacion, use_container_width=True)
    if not API_KEY:
        st.error("Falta la API key: defina ATV_API_KEY o API_KEY en el .env del proyecto.")
    st.caption(f"Sesión: `{st.session_state.sesion_id or '(nueva)'}`")
    if st.session_state.perfil:
        st.subheader("Perfil acumulado")
        st.json(st.session_state.perfil, expanded=False)
    st.divider()
    if st.button("Recargar datos de las tablas", use_container_width=True):
        st.cache_data.clear()
    st.caption(f"Las pestañas de datos se refrescan solas cada {CACHE_TTL_S} s.")


# --- Pestaña: Chat --------------------------------------------------------------
def consultar(texto: str) -> dict:
    cuerpo = {"consulta": texto, "vendedor_id": vendedor_id or "web", "sesion_id": st.session_state.sesion_id}
    if sucursal.strip().isdigit():
        cuerpo["sucursal_id"] = int(sucursal)
    r = requests.post(
        f"{url.rstrip('/')}/api/v1/consultar",
        json=cuerpo, headers={"X-API-Key": API_KEY or ""}, timeout=TIMEOUT_S,
    )
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:500]}")
    return r.json()


def mostrar_detalle_respuesta(datos: dict):
    columnas = st.columns(3)
    columnas[0].caption(f"Etapa: **{datos.get('etapa')}**")
    columnas[1].caption(f"Latencia: **{datos.get('latencia_ms')} ms**")
    columnas[2].caption(f"Escalada HITL: **{'sí' if datos.get('escalada_hitl') else 'no'}**")
    if datos.get("fuente"):
        st.caption(f"Fuente: {datos['fuente']}")
    if datos.get("carrito"):
        st.dataframe(datos["carrito"], hide_index=True)


def pestana_chat():
    st.caption("Chat contra la API. Las conversaciones quedan registradas en atv.consulta / atv.sesion_perfil.")
    historial = st.container()
    with historial:
        for m in st.session_state.mensajes:
            with st.chat_message(m["rol"]):
                st.markdown(m["texto"])
                if m.get("datos") and mostrar_detalle:
                    mostrar_detalle_respuesta(m["datos"])

    if pregunta := st.chat_input("Escriba su mensaje…", disabled=not API_KEY):
        st.session_state.mensajes.append({"rol": "user", "texto": pregunta})
        with historial:
            with st.chat_message("user"):
                st.markdown(pregunta)
            with st.chat_message("assistant"):
                try:
                    with st.spinner("Consultando al asistente…"):
                        datos = consultar(pregunta)
                except Exception as exc:
                    texto, datos = f"⚠️ Error al llamar a la API: {exc}", None
                else:
                    texto = datos.get("respuesta", "")
                    st.session_state.sesion_id = datos.get("sesion_id") or st.session_state.sesion_id
                    st.session_state.perfil = datos.get("perfil")
                st.markdown(texto)
        st.session_state.mensajes.append({"rol": "assistant", "texto": texto, "datos": datos})
        st.rerun()  # refresca la barra lateral (sesión y perfil)


# --- Pestaña: Archivos del RAG ------------------------------------------------------
def pestana_archivos():
    st.caption("Lo que la ingesta trajo de SeaweedFS (atv.inventario_archivo) y cuántos fragmentos "
               f"de cada documento hay en Qdrant (colección `{QDRANT_COLLECTION}`). Solo PDF de "
               "«Fichas Técnicas de Productos»: el resto del bucket no se ingesta.")
    try:
        inv = cargar_inventario()
    except Exception as exc:
        st.error(f"No se pudo leer atv.inventario_archivo: {exc}")
        return
    try:
        qd = cargar_fragmentos_qdrant()
    except Exception as exc:
        st.warning(f"No se pudo leer Qdrant ({type(exc).__name__}); se muestra solo el inventario.")
        qd = pd.DataFrame(columns=["documento", "fragmentos", "productos"])

    partes = inv["nombre_archivo"].str.split("/")
    inv["documento"] = partes.str[-1]
    inv["carpeta"] = partes.str[-2]
    inv = inv.merge(qd, on="documento", how="left")
    inv["fragmentos"] = inv["fragmentos"].fillna(0).astype(int)

    c = st.columns(4)
    c[0].metric("Archivos en inventario", len(inv))
    c[1].metric("Indexados", int((inv["estado"] == "INDEXADO").sum()))
    c[2].metric("Con error", int((inv["estado"] == "ERROR").sum()))
    c[3].metric("Fragmentos en Qdrant", int(qd["fragmentos"].sum()) if len(qd) else 0)

    f = st.columns([1, 1, 2])
    estados = f[0].multiselect("Estado", sorted(inv["estado"].unique()), default=[])
    carpetas = f[1].multiselect("Carpeta / proveedor", sorted(inv["carpeta"].dropna().unique()), default=[])
    buscar = f[2].text_input("Buscar archivo o producto", "")
    vista = inv
    if estados:
        vista = vista[vista["estado"].isin(estados)]
    if carpetas:
        vista = vista[vista["carpeta"].isin(carpetas)]
    if buscar:
        patron = re.escape(buscar)
        vista = vista[vista["documento"].str.contains(patron, case=False, na=False)
                      | vista["productos"].str.contains(patron, case=False, na=False)]

    st.dataframe(
        vista[["documento", "carpeta", "estado", "metodo_extraccion", "total_registros_atomicos",
               "fragmentos", "productos", "version_corpus", "ts_cargado", "ts_indexado"]],
        hide_index=True, use_container_width=True,
        column_config={
            "total_registros_atomicos": st.column_config.NumberColumn("registros"),
            "ts_cargado": st.column_config.DatetimeColumn("cargado", format="YYYY-MM-DD HH:mm"),
            "ts_indexado": st.column_config.DatetimeColumn("indexado", format="YYYY-MM-DD HH:mm"),
        },
    )
    st.caption(f"{len(vista)} de {len(inv)} archivos.")

    huerfanos = set(qd["documento"]) - set(inv["documento"])
    if huerfanos:
        st.warning(f"{len(huerfanos)} documento(s) están en Qdrant pero no en el inventario: "
                   + ", ".join(sorted(map(str, huerfanos))))


# --- Pestaña: Preguntas frecuentes ------------------------------------------------
def pestana_preguntas():
    st.caption("atv.pregunta_frecuente (carga: scripts/cargar_preguntas_frecuentes.py). "
               "«usos» = respuestas del agente que salieron de esa pregunta (atv.consulta).")
    try:
        faq = cargar_preguntas()
    except Exception as exc:
        st.error(f"No se pudo leer atv.pregunta_frecuente: {exc}")
        return
    if faq.empty:
        st.info("La tabla está vacía.")
        return

    c = st.columns(4)
    c[0].metric("Preguntas", len(faq))
    c[1].metric("Activas", int(faq["activo"].sum()))
    c[2].metric("Usadas alguna vez", int((faq["usos"] > 0).sum()))
    c[3].metric("Respuestas desde FAQ", int(faq["usos"].sum()))

    f = st.columns([1, 1, 1, 2])
    cultivos = f[0].multiselect("Cultivo", sorted(faq["cultivo"].dropna().unique()))
    categorias = f[1].multiselect("Categoría", sorted(faq["categoria"].dropna().unique()))
    solo_activas = f[2].checkbox("Solo activas", value=True)
    buscar = f[3].text_input("Buscar en pregunta, variantes o respuesta", "")
    vista = faq
    if cultivos:
        vista = vista[vista["cultivo"].isin(cultivos)]
    if categorias:
        vista = vista[vista["categoria"].isin(categorias)]
    if solo_activas:
        vista = vista[vista["activo"]]
    if buscar:
        patron = re.escape(buscar)
        vista = vista[vista[["pregunta", "variantes", "respuesta"]].apply(
            lambda col: col.str.contains(patron, case=False, na=False)).any(axis=1)]

    st.dataframe(
        vista[["codigo", "cultivo", "categoria", "pregunta", "variantes", "usos", "ultimo_uso", "activo"]],
        hide_index=True, use_container_width=True,
        column_config={"ultimo_uso": st.column_config.DatetimeColumn("último uso", format="YYYY-MM-DD HH:mm")},
    )
    st.caption(f"{len(vista)} de {len(faq)} preguntas. Origen: {', '.join(faq['archivo_origen'].dropna().unique())}")

    if not vista.empty:
        codigo = st.selectbox("Ver detalle", vista["codigo"],
                              format_func=lambda c: f"{c} · {vista.set_index('codigo').at[c, 'pregunta']}")
        fila = vista.set_index("codigo").loc[codigo]
        st.markdown(f"**{codigo}** · {fila['cultivo']} · {fila['categoria']}")
        st.markdown(f"**Pregunta:** {fila['pregunta']}")
        if fila["variantes"]:
            st.markdown(f"**Otras formas:** {fila['variantes']}")
        st.success(f"**Respuesta aprobada:** {fila['respuesta']}")
        if fila["respuesta_evitar"]:
            st.error(f"**Respuesta a evitar:** {fila['respuesta_evitar']}")


st.title("Asistente Técnico Virtual · AldeaZON")
tab_chat, tab_archivos, tab_faq = st.tabs(["💬 Chat", "📄 Archivos del RAG", "❓ Preguntas frecuentes"])
with tab_chat:
    pestana_chat()
with tab_archivos:
    pestana_archivos()
with tab_faq:
    pestana_preguntas()
