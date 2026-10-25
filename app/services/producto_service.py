# app/services/producto_service.py
"""
Cierre de cada respuesta con un producto de AldeaZON ("Producto AldeaZON recomendado: ...").

Por qué no lo escribe el LLM de generación: con la regla en el system_prompt
(scripts/006-008) gpt-4o-mini fue inestable (medido el 2026-10-17, corpus v2):
recomendó un HERBICIDA para mosca blanca, y con la regla endurecida dejó de
recomendar EVITO T para roya teniéndolo en el contexto, o nombró un producto en
el texto y otro en la línea final.

Ahora:
  1. elegir_producto: una llamada aparte (temperature 0, JSON) elige, entre las
     fichas recuperadas, la que sirve para el MISMO problema y cultivo, o ninguna
     (producto_selector_prompt). Corre en paralelo con la generación.
  2. linea_producto: la línea la arma el código con los datos de la ficha
     (producto, uso, dosis literal de la ficha); sin ficha que sirva, el mensaje
     de la tienda (msg_producto_tienda). Las dos plantillas viven en prompt_config.
  3. cerrar_con_producto: quita cualquier línea de producto que haya escrito el
     LLM y pone la del sistema antes de la fuente.
"""
import json
import logging
import re
import unicodedata
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.prompt_config import PromptConfig

logger = logging.getLogger("atv.producto")

PREFIJO_LINEA = "Producto AldeaZON recomendado:"

# Siglas de formulación que no forman parte del nombre comercial ("Mancuerna Plus 28 SE").
_FORMULACIONES = {"sc", "ec", "sl", "wp", "wg", "se", "od", "gr", "gb", "sp", "fs", "ew", "cs", "ft", "tds"}
# Para cruzar ficha e inventario por "uno contiene al otro" el nombre contenido debe tener al
# menos este largo: la ficha dice "ALDEAZON NPK PLUS+" y el inventario "NPK Plus"; la ficha
# "FLUAZIFOP-P-BUTYL 15 EC" y el inventario "Fluazifop 15EC" (medido el 2026-10-24).
LARGO_MIN_CONTENIDO = 6
REGEX_LINEA_LLM = re.compile(r"^\s*\**\s*Producto AldeaZON recomendado\s*:?.*$", re.IGNORECASE | re.MULTILINE)
REGEX_FUENTE = re.compile(r"\[\s*Fuente\s*:", re.IGNORECASE)
# extractor_ficha guarda "Dosis: <dosis>. <condiciones>"; la dosis termina en un punto seguido de
# mayúscula (la frase siguiente) o del final: así no corta en abreviaturas ("2 ml por Lt. de agua").
REGEX_DOSIS_EN_CONDICIONES = re.compile(r"^Dosis:\s*(.+?)\.(?=\s+[A-ZÁÉÍÓÚÑ¿(]|\s*$)")
MAX_CONDICIONES_SELECTOR = 160


def _texto(valor: Any) -> Optional[str]:
    texto = str(valor).strip() if valor is not None else ""
    return texto if texto and texto.lower() not in {"none", "null"} else None


def _mensaje_selector(consulta: str, cultivo: Optional[str], fichas: List[Dict[str, Any]]) -> str:
    lineas = [f"CONSULTA: {consulta}", f"CULTIVO DEL CLIENTE: {cultivo or 'no se sabe'}", "FICHAS:"]
    for i, f in enumerate(fichas):
        condiciones = (_texto(f.get("condiciones")) or "")[:MAX_CONDICIONES_SELECTOR]
        lineas.append(
            f"{i}. {f.get('producto')} | ingrediente activo: {_texto(f.get('principio_activo')) or '-'} | "
            f"cultivo: {_texto(f.get('cultivo')) or '-'} | "
            f"uso: {_texto(f.get('objetivo')) or '-'} | dosis: {_texto(f.get('dosis')) or '-'} | {condiciones}"
        )
    return "\n".join(lineas)


async def elegir_producto(
    llm_client, cfg: PromptConfig, consulta: str, cultivo: Optional[str], fichas: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """La ficha del producto a recomendar, o None. Falla cerrado (None → mensaje de la tienda)."""
    fichas = [f for f in fichas if _texto(f.get("producto"))]
    if not fichas:
        return None
    try:
        response = await llm_client.chat.completions.create(
            model=cfg.llm_modelo,
            messages=[
                {"role": "system", "content": cfg.producto_selector_prompt},
                {"role": "user", "content": _mensaje_selector(consulta, cultivo, fichas)},
            ],
            temperature=0,
            max_tokens=20,
            response_format={"type": "json_object"},
            timeout=settings.llm_timeout_seconds,
        )
        indice = json.loads(response.choices[0].message.content).get("indice")
    except Exception as exc:
        logger.warning(f"Product selector failed closed ({type(exc).__name__}): {exc}")
        return None
    # Solo vale un índice de la lista: el LLM no puede inventar un producto.
    if isinstance(indice, bool) or not isinstance(indice, int) or not 0 <= indice < len(fichas):
        return None
    return fichas[indice]


def linea_producto(ficha: Optional[Dict[str, Any]], cfg: PromptConfig) -> str:
    if ficha is None:
        return cfg.msg_producto_tienda
    objetivo, cultivo = _texto(ficha.get("objetivo")), _texto(ficha.get("cultivo"))
    if objetivo and objetivo.lower().startswith("ficha general"):
        objetivo = None
    uso = " en ".join(p for p in (objetivo, cultivo) if p) or "según su ficha técnica"
    dosis = _texto(ficha.get("dosis"))
    if not dosis and (m := REGEX_DOSIS_EN_CONDICIONES.match(_texto(ficha.get("condiciones")) or "")):
        dosis = m.group(1)
    return cfg.producto_linea_template.format(
        producto=_texto(ficha.get("producto")),
        uso=uso,
        dosis=f" Dosis según la ficha: {dosis}." if dosis else "",
        documento=_texto(ficha.get("documento_fuente")) or "ficha técnica",
    )


def cerrar_con_producto(respuesta: str, linea: str) -> str:
    """Quita las líneas de producto que haya escrito el LLM y pone la del sistema antes de la fuente."""
    texto = re.sub(r"\n{3,}", "\n\n", REGEX_LINEA_LLM.sub("", respuesta)).strip()
    fuentes = list(REGEX_FUENTE.finditer(texto))
    if not fuentes:
        return f"{texto}\n\n{linea}"
    corte = fuentes[-1].start()
    return f"{texto[:corte].rstrip()}\n\n{linea}\n\n{texto[corte:].lstrip()}"


# ---- Nombre de producto e inventario -------------------------------------------------------
def _clave_nombre(texto: str) -> str:
    sin_tildes = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", sin_tildes.lower())


def nombre_corto_producto(producto: str) -> str:
    """'MANCUERNA PLUS 28 SE' -> 'mancuernaplus'; 'ROOT-PLUS' -> 'rootplus'; 'Evito T 450 SC' -> 'evitot'."""
    palabras = [w for w in re.split(r"[\s\-_/®™]+", producto or "")
                if w and not any(ch.isdigit() for ch in w) and w.lower() not in _FORMULACIONES]
    return _clave_nombre("".join(palabras))


# ---- Ingredientes activos ------------------------------------------------------------------
# Palabras que no identifican al ingrediente ("Imidacloprid SC 200 (100+100 g/L)", "Emamectin Benzoato").
_PALABRAS_NO_INGREDIENTE = {"sc", "ec", "sl", "wp", "wg", "se", "od", "gr", "gl", "kg", "ml", "lt", "litro", "litros",
                            "grado", "tecnico", "acido", "sales", "sal", "base", "equivalente", "ingrediente", "activo",
                            "producto", "formulado", "benzonitrilo", "halogenado", "triazol", "metoxicrilato"}
LARGO_MIN_INGREDIENTE = 5


def clave_ingrediente(palabra: str) -> str:
    """
    Normaliza la ortografía inglés/español de un ingrediente para cruzar ficha y tabla de
    productos: 'Chlorothalonil' = 'Clorotalonil', 'AZOXYSTROBIN' = 'Azoxystrobina',
    'Cymoxanil' = 'Cimoxanilo', 'Tebuconazole' = 'Tebuconazol'.
    """
    w = unicodedata.normalize("NFKD", palabra or "").encode("ascii", "ignore").decode().lower()
    w = re.sub(r"[^a-z]", "", w)
    for a, b in (("ph", "f"), ("th", "t"), ("ch", "c"), ("qu", "c"), ("y", "i"), ("k", "c"), ("z", "s"), ("h", "")):
        w = w.replace(a, b)
    w = re.sub(r"(.)\1+", r"\1", w)          # letras dobles
    return re.sub(r"[aeo]+$", "", w)         # terminación: -a, -e, -o


def claves_ingrediente(nombre: str) -> List[str]:
    """Palabras significativas normalizadas de un nombre de ingrediente o de un 'principio activo' de ficha."""
    palabras = []
    for w in re.split(r"[^A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", nombre or ""):
        if w.lower() in _PALABRAS_NO_INGREDIENTE:
            continue
        clave = clave_ingrediente(w)
        if len(clave) >= LARGO_MIN_INGREDIENTE and clave not in _PALABRAS_NO_INGREDIENTE:
            palabras.append(clave)
    return palabras


# ---- Existencias ------------------------------------------------------------------------------
_COLUMNAS = "producto_id, producto_nombre, bodega_nombre, sucursal_id, unidad, cantidad_disponible, ingredientes, ingredientes_clave"
SQL_POR_NOMBRE = text(f"""
SELECT {_COLUMNAS}
  FROM atv.inventario_producto
 WHERE (nombre_clave = :clave
        OR (length(:clave) >= :minimo AND position(:clave IN nombre_clave) > 0)
        OR (length(nombre_clave) >= :minimo AND position(nombre_clave IN :clave) > 0))
""")
# Un ingrediente del producto coincide si TODAS sus palabras están entre las del principio
# activo de la ficha ("emamectin benzoat" exige las dos).
SQL_POR_INGREDIENTE = text(f"""
SELECT {_COLUMNAS}
  FROM atv.inventario_producto
 WHERE cantidad_disponible > 0
   AND EXISTS (SELECT 1 FROM unnest(ingredientes_clave) AS k
                WHERE k <> '' AND string_to_array(k, ' ') <@ CAST(:palabras AS text[]))
""")
SQL_HAY_INVENTARIO = text("SELECT EXISTS (SELECT 1 FROM atv.inventario_producto)")


def _cantidad(valor: float, unidad: Optional[str]) -> str:
    numero = f"{valor:,.0f}".replace(",", ".") if float(valor).is_integer() else f"{valor:,.2f}"
    if not unidad:
        return numero
    # "23 Litros"; con presentación ("Frasco 250 ML") va como "500 × Frasco 250 ML"
    return f"{numero} × {unidad}" if any(ch.isdigit() for ch in unidad) else f"{numero} {unidad}"


def resumir_existencias(filas: List[Dict[str, Any]], max_tiendas: int, sucursal_id: Optional[int]) -> str:
    """'Pantasma: 23 Litros; San Rafael: 12 Litros y 3 tiendas más' (la sucursal del cliente primero)."""
    tiendas: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    for f in filas:
        cantidad = float(f["cantidad_disponible"])
        if cantidad <= 0:
            continue
        t = tiendas.setdefault(f["bodega_nombre"], {"sucursal_id": f.get("sucursal_id"), "total": 0.0, "por_unidad": {}})
        t["total"] += cantidad
        t["por_unidad"][f.get("unidad")] = t["por_unidad"].get(f.get("unidad"), 0.0) + cantidad
    orden = sorted(tiendas.items(), key=lambda kv: (kv[1]["sucursal_id"] != sucursal_id or sucursal_id is None,
                                                     -kv[1]["total"], kv[0]))
    partes = [f"{nombre}: " + " + ".join(_cantidad(c, u) for u, c in sorted(d["por_unidad"].items(), key=lambda x: -x[1]))
              for nombre, d in orden[:max_tiendas]]
    resto = len(orden) - max_tiendas
    texto = "; ".join(partes)
    return f"{texto} y {resto} tienda{'s' if resto > 1 else ''} más" if resto > 0 else texto


def _por_producto(filas: List[Dict[str, Any]]) -> "OrderedDict[str, List[Dict[str, Any]]]":
    """Agrupa por nombre comercial (las presentaciones de un mismo producto van juntas)."""
    grupos: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    for f in filas:
        grupos.setdefault(f["producto_nombre"], []).append(f)
    return grupos


def nombre_ingrediente_visible(nombre: str) -> str:
    """'Imidacloprid SC 200 (100+100 g/L)' -> 'Imidacloprid' (sin formulación ni concentración)."""
    palabras = []
    for w in (nombre or "").split():
        if "(" in w or any(ch.isdigit() for ch in w) or w.lower() in _FORMULACIONES:
            break
        palabras.append(w)
    return " ".join(palabras) or (nombre or "").strip()


def _ingredientes_comunes(filas: List[Dict[str, Any]], palabras: set) -> List[str]:
    comunes = []
    for f in filas:
        for nombre, clave in zip(f.get("ingredientes") or [], f.get("ingredientes_clave") or []):
            visible = nombre_ingrediente_visible(nombre)
            if clave and set(clave.split()) <= palabras and visible not in comunes:
                comunes.append(visible)
    return comunes


def formatear_alternativas(filas: List[Dict[str, Any]], palabras: set, cfg: PromptConfig,
                           sucursal_id: Optional[int]) -> Optional[str]:
    grupos = sorted(_por_producto(filas).items(),
                    key=lambda kv: -sum(float(f["cantidad_disponible"]) for f in kv[1]))[:cfg.inventario_max_productos]
    if not grupos:
        return None
    ingredientes = []
    lineas = []
    for nombre, fs in grupos:
        comunes = _ingredientes_comunes(fs, palabras)
        ingredientes += [i for i in comunes if i not in ingredientes]
        lineas.append(f"- {nombre}" + (f" ({', '.join(comunes)})" if comunes else "")
                      + f": {resumir_existencias(fs, cfg.inventario_max_tiendas, sucursal_id)}")
    return cfg.inventario_alternativas_template.format(
        ingredientes=", ".join(ingredientes) or "el mismo ingrediente", productos="\n".join(lineas)
    )


async def texto_existencias(
    session: AsyncSession, ficha: Optional[Dict[str, Any]], cfg: PromptConfig, sucursal_id: Optional[int] = None
) -> Optional[str]:
    """
    Cierre de inventario del producto recomendado (atv.inventario_producto, scripts/011-012):
      1. sus existencias por tienda (cruce por nombre: "EVITO T" = "Evito T 450 SC");
      2. otros productos de la tabla de productos con su mismo ingrediente activo (tomado de la
         ficha técnica: "Fluoxastrobin + Tebuconazole"), con sus existencias.
    None si no hay producto o si el inventario está desactivado (inventario_max_tiendas = 0).
    Si el inventario aún no está cargado o falla la consulta: msg_inventario_no_disponible
    (nunca afirma "no hay existencias" sin inventario cargado).
    """
    if not ficha or cfg.inventario_max_tiendas <= 0:
        return None
    clave = nombre_corto_producto(_texto(ficha.get("producto")) or "")
    palabras = sorted(set(claves_ingrediente(_texto(ficha.get("principio_activo")) or "")))
    try:
        if not (await session.execute(SQL_HAY_INVENTARIO)).scalar():
            return cfg.msg_inventario_no_disponible
        propias = [dict(r) for r in (await session.execute(
            SQL_POR_NOMBRE, {"clave": clave, "minimo": LARGO_MIN_CONTENIDO}
        )).mappings()] if len(clave) >= 4 else []
        mismas = [dict(r) for r in (await session.execute(
            SQL_POR_INGREDIENTE, {"palabras": palabras}
        )).mappings()] if palabras and cfg.inventario_max_productos > 0 else []
    except Exception as exc:
        logger.warning(f"Inventory lookup failed ({type(exc).__name__}): {exc}")
        await session.rollback()
        return cfg.msg_inventario_no_disponible

    con_stock = [f for f in propias if float(f["cantidad_disponible"]) > 0]
    partes = [cfg.inventario_template.format(tiendas=resumir_existencias(con_stock, cfg.inventario_max_tiendas, sucursal_id))
              if con_stock else cfg.msg_sin_existencias]
    ids_propios = {f["producto_id"] for f in propias}
    alternativas = formatear_alternativas([f for f in mismas if f["producto_id"] not in ids_propios],
                                          set(palabras), cfg, sucursal_id)
    if alternativas:
        partes.append("\n" + alternativas)
    return " ".join(partes[:1]) + "".join(partes[1:])
