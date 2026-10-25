# tools/chat_streamlit/app.py
"""
Consola de pruebas del ATV (equipo de desarrollo, no es la web de AldeaZON).

    .venv-streamlit/bin/streamlit run tools/chat_streamlit/app.py --server.address 127.0.0.1

Pestañas:
  - Chat: POST /api/v1/consultar, con un sesion_id propio por conversación.
  - Formulario inicial: las preguntas de la bienvenida en un formulario, enviadas
    en un solo mensaje. Chat y Formulario muestran tarjetas con los tokens y el
    costo de la conversación (consumo_llm de cada respuesta), para compararlos.
  - Archivos del RAG: atv.inventario_archivo (lo que la ingesta trajo de SeaweedFS)
    + fragmentos por documento en Qdrant.
  - Preguntas frecuentes: atv.pregunta_frecuente + cuántas veces las usó el agente.

Las credenciales salen de ATV_* o del .env del proyecto y nunca se muestran.
Postgres se abre en modo solo lectura y Qdrant solo se lee (scroll).
"""
import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

import altair as alt
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
def leer_sql(sql: str, params: tuple | None = None) -> pd.DataFrame:
    # Sin params psycopg no interpreta '%' (LIKE '%faq%' en cargar_preguntas).
    with psycopg.connect(DATABASE_URL, connect_timeout=10) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute(sql, params)
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


# --- Conversaciones -------------------------------------------------------------
# Dos conversaciones independientes, cada una con su sesión y su consumo, para
# comparar el chat libre con el formulario inicial.
CONVERSACIONES = {"chat": "💬 Chat", "form": "📝 Formulario inicial"}


def consumo_vacio() -> dict:
    return {"mensajes": 0, "turnos_perfil": 0, "llamadas": 0, "tokens_entrada": 0, "tokens_salida": 0,
            "costo_usd": 0.0, "latencia_ms": 0, "por_componente": {}, "por_paso": {}}


# Las conversaciones se guardan en disco, ligadas a ?c=<id> en la URL: si el navegador
# recarga la página o pierde el websocket (p. ej. el reenvío de puertos del IDE), la
# sesión de Streamlit es nueva y sin esto se perdería lo conversado.
DIR_CONVERSACIONES = Path(tempfile.gettempdir()) / "atv_streamlit_conversaciones"


def _archivo_conversaciones() -> Path:
    cid = st.query_params.get("c", "")
    if not re.fullmatch(r"[0-9a-f]{32}", cid):
        cid = uuid.uuid4().hex
        st.query_params["c"] = cid
    return DIR_CONVERSACIONES / f"{cid}.json"


def guardar_conversaciones():
    try:
        DIR_CONVERSACIONES.mkdir(mode=0o700, exist_ok=True)
        archivo = _archivo_conversaciones()
        archivo.write_text(json.dumps({c: st.session_state[c] for c in CONVERSACIONES}, ensure_ascii=False, default=str))
        archivo.chmod(0o600)
    except Exception as exc:  # sin disco la consola sigue, solo pierde la recuperación
        print(f"[conversaciones] no se pudo guardar: {exc}", flush=True)


def nueva_conversacion(conv: str, guardar: bool = True):
    st.session_state[conv] = {"mensajes": [], "sesion_id": None, "perfil": None, "consumo": consumo_vacio()}
    if guardar:
        guardar_conversaciones()


def restaurar_conversaciones():
    """Al abrir la página: las conversaciones de esta URL (?c=...), si ya existían."""
    try:
        guardadas = json.loads(_archivo_conversaciones().read_text())
    except (OSError, ValueError):
        guardadas = {}
    for conv in CONVERSACIONES:
        if conv not in st.session_state:
            if conv in guardadas:
                st.session_state[conv] = guardadas[conv]
            else:
                nueva_conversacion(conv, guardar=False)


restaurar_conversaciones()

with st.sidebar:
    st.header("API")
    url = st.text_input("URL de la API", URL_POR_DEFECTO)
    vendedor_id = st.text_input("vendedor_id", "web")
    sucursal = st.text_input("sucursal_id (opcional)", "")
    mostrar_detalle = st.checkbox("Mostrar detalle técnico", value=True)
    if not API_KEY:
        st.error("Falta la API key: defina ATV_API_KEY o API_KEY en el .env del proyecto.")
    st.divider()
    st.subheader("Comparación")
    st.dataframe(pd.DataFrame(
        [{"conversación": nombre, "mensajes": st.session_state[c]["consumo"]["mensajes"],
          "tokens": st.session_state[c]["consumo"]["tokens_entrada"] + st.session_state[c]["consumo"]["tokens_salida"],
          "costo USD": round(st.session_state[c]["consumo"]["costo_usd"], 5)}
         for c, nombre in CONVERSACIONES.items()]).set_index("conversación"), use_container_width=True)
    st.divider()
    if st.button("Recargar datos de las tablas", use_container_width=True):
        st.cache_data.clear()
    st.caption(f"Las pestañas de datos se refrescan solas cada {CACHE_TTL_S} s.")


def consultar(conv: str, texto: str, contexto: dict | None = None) -> dict:
    estado = st.session_state[conv]
    cuerpo = {"consulta": texto, "vendedor_id": vendedor_id or "web", "sesion_id": estado["sesion_id"]}
    if contexto:
        cuerpo["contexto"] = contexto
    if sucursal.strip().isdigit():
        cuerpo["sucursal_id"] = int(sucursal)
    r = requests.post(
        f"{url.rstrip('/')}/api/v1/consultar",
        json=cuerpo, headers={"X-API-Key": API_KEY or ""}, timeout=TIMEOUT_S,
    )
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:500]}")
    return r.json()


def acumular_consumo(conv: str, datos: dict):
    c = st.session_state[conv]["consumo"]
    uso = datos.get("consumo_llm") or {}
    c["mensajes"] += 1
    c["turnos_perfil"] += datos.get("etapa") == "perfilamiento"
    c["llamadas"] += uso.get("llamadas", 0)
    c["tokens_entrada"] += uso.get("tokens_entrada", 0)
    c["tokens_salida"] += uso.get("tokens_salida", 0)
    c["costo_usd"] += uso.get("costo_usd", 0.0)
    c["latencia_ms"] += datos.get("latencia_ms") or 0
    traza = datos.get("latencia_pasos") or {}
    for comp, ms in (traza.get("por_componente") or {}).items():
        c["por_componente"][comp] = c["por_componente"].get(comp, 0) + ms
    for p in traza.get("pasos") or []:
        c["por_paso"][p["paso"]] = c["por_paso"].get(p["paso"], 0) + p["ms"]


def tarjetas_consumo(conv: str):
    c = st.session_state[conv]["consumo"]
    col = st.columns(5)
    col[0].metric("Mensajes enviados", c["mensajes"], help=f"{c['turnos_perfil']} de perfilamiento")
    col[1].metric("Llamadas al LLM", c["llamadas"])
    col[2].metric("Tokens", f"{c['tokens_entrada'] + c['tokens_salida']:,}".replace(",", "."),
                  help=f"Entrada: {c['tokens_entrada']:,} · Salida: {c['tokens_salida']:,}".replace(",", "."))
    col[3].metric("Consumo API (USD)", f"${c['costo_usd']:.5f}", help="gpt-4o-mini: 0,15 USD/M de entrada, 0,60 USD/M de salida")
    col[4].metric("Latencia total", f"{c['latencia_ms'] / 1000:.1f} s")
    st.caption(f"Sesión: `{st.session_state[conv]['sesion_id'] or '(nueva)'}` · cuenta todas las llamadas "
               "al LLM de cada mensaje (perfil, alcance, juez de FAQ, selector de producto y respuesta).")
    latencia_conversacion(conv)


# --- Latencia por paso ----------------------------------------------------------
# Colores fijos por componente (app/services/traza.py), iguales en todos los gráficos.
COLORES_COMPONENTE = {"llm": "#d1495b", "embedding": "#edae49", "qdrant": "#00798c",
                      "postgres": "#30638e", "redis": "#8f2d56", "cpu": "#7a7a7a"}
ESCALA_COLOR = alt.Scale(domain=list(COLORES_COMPONENTE), range=list(COLORES_COMPONENTE.values()))


def latencia_conversacion(conv: str):
    """Dónde se fue el tiempo en toda la conversación: por componente y los pasos más lentos."""
    c = st.session_state[conv]["consumo"]
    if not c["por_componente"]:
        return
    with st.expander("⏱️ Latencia de la conversación por componente y por paso", expanded=False):
        total = sum(c["por_componente"].values()) or 1
        comp = pd.DataFrame([{"componente": k, "ms": round(v), "%": round(100 * v / total, 1)}
                             for k, v in sorted(c["por_componente"].items(), key=lambda kv: -kv[1])])
        pasos = pd.DataFrame([{"paso": k, "ms": round(v)} for k, v in
                              sorted(c["por_paso"].items(), key=lambda kv: -kv[1])])
        col = st.columns([1, 2])
        col[0].dataframe(comp, hide_index=True, use_container_width=True)
        col[1].dataframe(pasos.head(8), hide_index=True, use_container_width=True)
        st.caption("Suma de la duración de cada paso. Los pasos en paralelo (p. ej. generación y selector de "
                   "producto) se suman los dos, por eso el total puede pasar la latencia de la conversación.")


def cascada_latencia(traza: dict):
    """Gráfico de cascada de un mensaje: cada paso con su inicio y su duración."""
    pasos = pd.DataFrame(traza.get("pasos") or [])
    if pasos.empty:
        return
    pasos["fin_ms"] = pasos["inicio_ms"] + pasos["ms"]
    pasos["orden"] = range(len(pasos))
    grafico = alt.Chart(pasos).mark_bar(cornerRadius=2).encode(
        x=alt.X("inicio_ms:Q", title="ms desde que llegó el mensaje"),
        x2="fin_ms:Q",
        y=alt.Y("paso:N", sort=alt.EncodingSortField(field="orden"), title=None),
        color=alt.Color("componente:N", scale=ESCALA_COLOR, legend=alt.Legend(orient="bottom")),
        tooltip=["paso", "componente", alt.Tooltip("ms:Q", title="duración (ms)"),
                 alt.Tooltip("inicio_ms:Q", title="inicio (ms)")],
    ).properties(height=max(120, 22 * len(pasos)))
    st.altair_chart(grafico, use_container_width=True)
    lento = pasos.loc[pasos["ms"].idxmax()]
    st.caption(f"Total {traza.get('total_ms', 0):.0f} ms · paso más lento: **{lento['paso']}** "
               f"({lento['ms']:.0f} ms, {lento['componente']}) · por componente: "
               + ", ".join(f"{k} {v:.0f} ms" for k, v in sorted((traza.get('por_componente') or {}).items(),
                                                                  key=lambda kv: -kv[1])))


def mostrar_detalle_respuesta(datos: dict):
    uso = datos.get("consumo_llm") or {}
    columnas = st.columns(4)
    columnas[0].caption(f"Etapa: **{datos.get('etapa')}**")
    columnas[1].caption(f"Latencia: **{datos.get('latencia_ms')} ms**")
    columnas[2].caption(f"LLM: **{uso.get('llamadas', 0)} llamadas · "
                        f"{uso.get('tokens_entrada', 0) + uso.get('tokens_salida', 0)} tokens**")
    columnas[3].caption(f"Escalada HITL: **{'sí' if datos.get('escalada_hitl') else 'no'}**")
    if datos.get("fuente"):
        st.caption(f"Fuente: {datos['fuente']}")
    if datos.get("carrito"):
        st.dataframe(datos["carrito"], hide_index=True)
    if datos.get("latencia_pasos"):
        with st.expander("⏱️ Latencia por paso de este mensaje"):
            cascada_latencia(datos["latencia_pasos"])


def llamar_api(conv: str, texto: str, consulta: str | None = None, contexto: dict | None = None) -> None:
    """
    Llama a la API y guarda la pregunta y la respuesta en el historial SIN llamar a st.*:
    si Streamlit interrumpe la ejecución mientras espera (rerun, reconexión del navegador),
    la interrupción llega en la siguiente llamada a st.*, cuando la respuesta ya quedó
    guardada, y aparece en la siguiente ejecución en vez de perderse.
    """
    estado = st.session_state[conv]
    try:
        datos = consultar(conv, consulta or texto, contexto)
    except Exception as exc:
        texto_resp, datos = f"⚠️ Error al llamar a la API: {exc}", None
    else:
        texto_resp = datos.get("respuesta", "")
        estado["sesion_id"] = datos.get("sesion_id") or estado["sesion_id"]
        estado["perfil"] = datos.get("perfil")
        acumular_consumo(conv, datos)
    estado["mensajes"] += [{"rol": "user", "texto": texto},
                           {"rol": "assistant", "texto": texto_resp, "datos": datos}]
    guardar_conversaciones()


def enviar(conv: str, texto: str, contenedor, consulta: str | None = None, contexto: dict | None = None):
    """
    Muestra el mensaje mientras se espera la respuesta; el historial lo escribe llamar_api.
    Con `contexto` (formulario estructurado) a la API va `consulta`; `texto` es lo que se muestra.
    """
    with contenedor:
        with st.chat_message("user"):
            st.markdown(texto)
        with st.spinner("Consultando al asistente…"):
            llamar_api(conv, texto, consulta, contexto)


def historial(conv: str):
    contenedor = st.container()
    with contenedor:
        for m in st.session_state[conv]["mensajes"]:
            with st.chat_message(m["rol"]):
                st.markdown(m["texto"])
                if m.get("datos") and mostrar_detalle:
                    mostrar_detalle_respuesta(m["datos"])
    return contenedor


def perfil_acumulado(conv: str):
    if st.session_state[conv]["perfil"]:
        with st.expander("Perfil acumulado"):
            st.json(st.session_state[conv]["perfil"])


# --- Pestaña: Chat --------------------------------------------------------------
def pestana_chat():
    tarjetas_consumo("chat")
    st.button("Nueva conversación", on_click=nueva_conversacion, args=("chat",), key="nueva_chat")
    st.caption("Chat libre contra la API: el agente pide los datos del caso conversando. "
               "Queda registrado en atv.consulta / atv.sesion_perfil.")
    contenedor = historial("chat")
    if pregunta := st.chat_input("Escriba su mensaje…", disabled=not API_KEY, key="entrada_chat"):
        enviar("chat", pregunta, contenedor)
        st.rerun()  # refresca tarjetas y comparación
    perfil_acumulado("chat")


# --- Pestaña: Formulario inicial --------------------------------------------------
ETAPAS = ["", "Siembra", "Desarrollo", "Floración", "Llenado de fruto / grano", "Cosecha", "Postcosecha"]
TOPOGRAFIAS = ["", "Plano", "Semiplano", "Semiquebrado", "Quebrado"]
DEDICACIONES = ["Productor", "Dueño de agroservicio", "Técnico agrícola", "Otro"]


def _frase(plantilla: str, valor) -> str:
    return plantilla.format(valor) if valor not in (None, "", 0, 0.0) else ""


TIPO_POR_DEDICACION = {"Productor": "productor", "Dueño de agroservicio": "comerciante",
                       "Técnico agrícola": "tecnico", "Otro": "otro"}


def consulta_formulario(socio: bool, d: dict) -> str:
    """El problema o la pregunta: lo único que va como texto cuando el perfil va estructurado."""
    if socio:
        return d["consulta"].strip()
    partes = [f"{d['sintomas'].strip().rstrip('.')}.", _frase("Desde hace {}.", d["desde"]),
              _frase("He aplicado: {}.", d["aplicado"]) or "No he aplicado nada."]
    return " ".join(p for p in partes if p)


def contexto_formulario(socio: bool, d: dict) -> dict:
    """ContextoInicial de la API: el perfil sin pasar por el LLM de extracción."""
    if socio:
        ctx = {"tipo_usuario": d["tipo"], "cedula": d["cedula"], "telefono": d["whatsapp"]}
    else:
        ctx = {"tipo_usuario": TIPO_POR_DEDICACION[d["dedicacion"]],
               "cultivos": [{"nombre": d["cultivo"].strip(), "area": d["manzanas"] or None, "unidad": "mz"}],
               "municipio": d["municipio"], "comunidad": d["comunidad"], "topografia": d["topografia"],
               "tipo_suelo": d["suelo"], "altura_msnm": d["altura"] or None}
    ctx["etapa_cultivo"] = d["etapa"]
    return {k: v for k, v in ctx.items() if v not in (None, "", [])}


def mensaje_formulario(socio: bool, d: dict) -> str:
    """Las respuestas del formulario, en un solo mensaje: lo que pide perfil_msg_bienvenida."""
    if socio:
        partes = [f"Soy {d['tipo']} de Aldea Global, cédula {d['cedula']}, WhatsApp {d['whatsapp']}.",
                  _frase("El cultivo está en {}.", d["etapa"].lower()), d["consulta"]]
    else:
        partes = [
            f"Soy {d['dedicacion'].lower()}, no soy socio ni cliente de Aldea Global.",
            f"Tengo {d['cultivo']}" + _frase(" ({:g} manzanas)", d["manzanas"]) + ".",
            "Mi finca está en " + ", ".join(x for x in (_frase("la comunidad {}", d["comunidad"]),
                                                       _frase("el municipio de {}", d["municipio"])) if x) + ".",
            _frase("El cultivo está en {}.", d["etapa"].lower()),
            _frase("Terreno: {}.", ", ".join(x for x in (d["topografia"].lower(), _frase("suelo {}", d["suelo"]),
                                                          _frase("{:g} msnm", d["altura"])) if x)),
            f"Problema: {d['sintomas'].strip().rstrip('.')}.",
            _frase("Desde hace {}.", d["desde"]),
            _frase("He aplicado: {}.", d["aplicado"]) or "No he aplicado nada.",
        ]
    return " ".join(p.strip() for p in partes if p and p.strip())


ETIQUETAS_OBLIGATORIAS = {"cedula": "Cédula", "whatsapp": "WhatsApp registrado", "consulta": "Consulta o problema",
                          "cultivo": "Cultivo", "municipio": "Municipio", "sintomas": "¿Qué problema tiene?"}


def formulario_inicial():
    st.markdown("Complete el caso de una vez: se envía **en un solo mensaje** al iniciar la conversación, "
                "con las mismas preguntas que hace el agente en la bienvenida.")
    socio = st.radio("¿Es socio o cliente de Aldea Global?", ["Sí", "No"], horizontal=True, key="form_socio") == "Sí"
    with st.form("form_inicial"):
        d = {}
        if socio:
            c = st.columns(3)
            d["tipo"] = c[0].selectbox("Es", ["socio", "cliente"])
            d["cedula"] = c[1].text_input("Cédula", placeholder="999-010180-0001A")
            d["whatsapp"] = c[2].text_input("WhatsApp registrado", placeholder="5555-0001")
            d["etapa"] = st.selectbox("Etapa del cultivo (opcional)", ETAPAS)
            d["consulta"] = st.text_area("Consulta o problema", placeholder="¿Qué tiene para la roya? Desde cuándo, qué ha aplicado…")
            obligatorios = ("cedula", "whatsapp", "consulta")
        else:
            c = st.columns(3)
            d["dedicacion"] = c[0].selectbox("¿A qué se dedica?", DEDICACIONES)
            d["cultivo"] = c[1].text_input("Cultivo", placeholder="Tomate")
            d["manzanas"] = c[2].number_input("Manzanas", min_value=0.0, step=0.5)
            c = st.columns(3)
            d["municipio"] = c[0].text_input("Municipio", placeholder="Sébaco")
            d["comunidad"] = c[1].text_input("Comunidad (opcional)")
            d["etapa"] = c[2].selectbox("Etapa del cultivo", ETAPAS)
            c = st.columns(3)
            d["topografia"] = c[0].selectbox("Terreno", TOPOGRAFIAS)
            d["suelo"] = c[1].text_input("Tipo de suelo (opcional)", placeholder="Franco arcilloso")
            d["altura"] = c[2].number_input("Altura (msnm, opcional)", min_value=0, step=50)
            d["sintomas"] = st.text_area("¿Qué problema tiene? ¿Qué le ve a la planta?",
                                         placeholder="Mosca blanca en el envés de las hojas, hojas amarillas…")
            c = st.columns(2)
            d["desde"] = c[0].text_input("¿Desde cuándo?", placeholder="2 semanas")
            d["aplicado"] = c[1].text_input("¿Qué ha aplicado?", placeholder="Nada")
            obligatorios = ("cultivo", "municipio", "sintomas")
        estructurado = st.checkbox(
            "Enviar como campos estructurados (sin el LLM que extrae el perfil)", value=True,
            help="Desmarcado: se manda todo como texto y la API extrae el perfil con el LLM, para comparar.")
        enviado = st.form_submit_button("Iniciar conversación", type="primary", disabled=not API_KEY)
    if enviado:
        faltan = [k for k in obligatorios if not str(d.get(k) or "").strip()]
        # Sin valores: solo qué campos llegaron llenos, para diagnosticar envíos vacíos.
        print(f"[formulario] socio={socio} llenos={sorted(k for k, v in d.items() if v)} faltan={faltan}", flush=True)
        if faltan:
            st.error("Falta: " + ", ".join(ETIQUETAS_OBLIGATORIAS[k] for k in faltan) + ". Si ya lo escribió, "
                     "pulse fuera del cuadro (o Ctrl+Enter) y vuelva a pulsar «Iniciar conversación».")
            return
        nueva_conversacion("form")
        if estructurado:
            enviar("form", mensaje_formulario(socio, d), st.container(),
                   consulta=consulta_formulario(socio, d), contexto=contexto_formulario(socio, d))
        else:
            enviar("form", mensaje_formulario(socio, d), st.container())
        st.rerun()


def pestana_formulario():
    tarjetas_consumo("form")
    if not st.session_state["form"]["mensajes"]:
        formulario_inicial()
        return
    st.button("Nuevo formulario", on_click=nueva_conversacion, args=("form",), key="nueva_form")
    contenedor = historial("form")
    if pregunta := st.chat_input("Siga la conversación…", disabled=not API_KEY, key="entrada_form"):
        enviar("form", pregunta, contenedor)
        st.rerun()
    perfil_acumulado("form")


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


# --- Pestaña: Prompts del agente -------------------------------------------------
# La consola solo lee Postgres. Para guardar llama a scripts/editar_prompt_config.py
# con el entorno de la API: la misma validación que la API y un historial de cambios.
PYTHON_API = RAIZ / ".venv" / "bin" / "python"
GRUPOS_PROMPT = {
    "Todos": None,
    "Respuesta (fichas)": ("system_prompt", "user_prompt_template", "chunk_template", "llm_", "retrieval_",
                           "msg_retrieval", "msg_baja", "msg_llm", "msg_groundedness"),
    "Perfilamiento": ("perfil_",),
    "Alcance": ("alcance_", "msg_fuera_alcance"),
    "Preguntas frecuentes": ("faq_",),
    "Producto e inventario": ("producto_", "msg_producto", "inventario_", "msg_sin_existencias", "msg_inventario"),
}


@st.cache_data(ttl=CACHE_TTL_S, show_spinner="Leyendo atv.prompt_config…")
def cargar_prompts() -> pd.DataFrame:
    return leer_sql("""SELECT clave, tipo, descripcion, valor, activo, ts_actualizado, actualizado_por
                         FROM atv.prompt_config ORDER BY clave""")


def cargar_historial(clave: str) -> pd.DataFrame:
    return leer_sql("""SELECT id, ts, autor, motivo, valor_anterior, valor_nuevo
                         FROM atv.prompt_config_historial WHERE clave = %s ORDER BY ts DESC LIMIT 20""", (clave,))


def editar_prompt(accion: str, datos: dict) -> dict:
    """validar | guardar, vía scripts/editar_prompt_config.py (entorno de la API)."""
    try:
        r = subprocess.run([str(PYTHON_API), "-m", "scripts.editar_prompt_config", accion],
                           input=json.dumps(datos, ensure_ascii=False), capture_output=True, text=True,
                           cwd=RAIZ, timeout=60)
    except Exception as exc:
        return {"ok": False, "error": f"No se pudo ejecutar el editor: {exc}"}
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": f"El editor falló: {(r.stderr.strip().splitlines() or ['sin detalle'])[-1]}"}


def pestana_prompts():
    st.caption("Configuración del agente en atv.prompt_config. La API la lee en cada mensaje: un cambio guardado "
               "rige desde el siguiente mensaje, sin reiniciar. Antes de guardar se valida igual que en la API "
               "(placeholders obligatorios de las plantillas, números válidos, textos no vacíos) y cada cambio "
               "queda en atv.prompt_config_historial.")
    try:
        prompts = cargar_prompts()
    except Exception as exc:
        st.error(f"No se pudo leer atv.prompt_config: {exc}")
        return

    f = st.columns([1, 2])
    grupo = f[0].selectbox("Grupo", list(GRUPOS_PROMPT))
    prefijos = GRUPOS_PROMPT[grupo]
    vista = prompts if prefijos is None else prompts[prompts["clave"].str.startswith(prefijos)]
    if vista.empty:
        st.info("No hay claves en este grupo.")
        return
    clave = f[1].selectbox("Clave", vista["clave"], format_func=lambda c: f"{c} · {vista.set_index('clave').at[c, 'tipo']}")
    fila = prompts.set_index("clave").loc[clave]

    st.markdown(f"**{clave}** · tipo `{fila['tipo']}` · {'activa' if fila['activo'] else '⚠️ inactiva'} · "
                f"última edición {pd.Timestamp(fila['ts_actualizado']):%Y-%m-%d %H:%M} "
                f"por {fila['actualizado_por'] or '—'}")
    if fila["descripcion"]:
        st.info(fila["descripcion"])

    alto = 80 if fila["tipo"] in ("int", "float") else min(600, max(150, 22 * (fila["valor"].count("\n") + 2)))
    valor = st.text_area("Valor", fila["valor"], height=alto, key=f"prompt_valor_{clave}")
    c = st.columns([1, 2, 1, 1])
    autor = c[0].text_input("Autor", "consola", key="prompt_autor", max_chars=64)
    motivo = c[1].text_input("Motivo del cambio", "", key=f"prompt_motivo_{clave}")
    c[2].write(""); c[3].write("")
    if c[2].button("Validar", key=f"prompt_validar_{clave}", use_container_width=True):
        r = editar_prompt("validar", {"clave": clave, "valor": valor})
        if r.get("ok"):
            st.success("Sin cambios." if r.get("sin_cambios") else "Válido: se puede guardar.")
        else:
            st.error(r.get("error"))
    if c[3].button("Guardar", key=f"prompt_guardar_{clave}", type="primary", use_container_width=True,
                   disabled=valor == fila["valor"]):
        r = editar_prompt("guardar", {"clave": clave, "valor": valor, "autor": autor, "motivo": motivo})
        if r.get("ok"):
            cargar_prompts.clear()
            st.success("Guardado. Rige desde el próximo mensaje al agente.")
        else:
            st.error(r.get("error"))

    historial_clave = cargar_historial(clave)
    with st.expander(f"Historial de cambios ({len(historial_clave)})"):
        if historial_clave.empty:
            st.caption("Sin cambios registrados desde la consola.")
            return
        st.dataframe(historial_clave[["id", "ts", "autor", "motivo"]], hide_index=True, use_container_width=True,
                     column_config={"ts": st.column_config.DatetimeColumn("cuándo", format="YYYY-MM-DD HH:mm")})
        cambio = st.selectbox("Ver cambio", historial_clave["id"], key=f"prompt_hist_{clave}",
                              format_func=lambda i: f"#{i}")
        h = historial_clave.set_index("id").loc[cambio]
        col = st.columns(2)
        col[0].text_area("Antes", h["valor_anterior"] or "", height=200, disabled=True, key=f"antes_{clave}_{cambio}")
        col[1].text_area("Después", h["valor_nuevo"], height=200, disabled=True, key=f"despues_{clave}_{cambio}")
        if h["valor_anterior"] is not None and st.button(f"Restaurar el valor anterior al cambio #{cambio}",
                                                         key=f"restaurar_{clave}_{cambio}"):
            r = editar_prompt("guardar", {"clave": clave, "valor": h["valor_anterior"], "autor": autor,
                                          "motivo": f"restaura el valor anterior al cambio #{cambio}"})
            if r.get("ok"):
                cargar_prompts.clear()
                st.session_state.pop(f"prompt_valor_{clave}", None)
                st.rerun()
            st.error(r.get("error"))


st.title("Asistente Técnico Virtual · AldeaZON")
tab_chat, tab_form, tab_archivos, tab_faq, tab_prompts = st.tabs(
    [CONVERSACIONES["chat"], CONVERSACIONES["form"], "📄 Archivos del RAG", "❓ Preguntas frecuentes",
     "⚙️ Prompts del agente"])
with tab_chat:
    pestana_chat()
with tab_form:
    pestana_formulario()
with tab_archivos:
    pestana_archivos()
with tab_faq:
    pestana_preguntas()
with tab_prompts:
    pestana_prompts()
