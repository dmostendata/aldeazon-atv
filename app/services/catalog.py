# app/services/catalog.py
import logging
from typing import List, Optional
from sqlalchemy import select, and_, column, table, asc
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.domain import ProductoComercial, CarritoItem

logger = logging.getLogger("atv.catalog")

# =====================================================================
# SQLALCHEMY 2.0 CORE TABLE METADATA (SCHEMA: stc2)
# =====================================================================
# LARAVEL BRIDGING:
# Defining explicit Core tables is equivalent to configuring a raw DB::table()
# contract without booting full Eloquent ORM models. This eliminates reflection
# and identity-map overhead in the asyncio event loop, keeping p95 latency low.
stc2_agroquimico = table(
    "agroquimico",
    column("id"),
    column("nombre"),
    column("ingrediente_activo"),
    column("estado"),
    column("disponible_propuesta"),
    schema="stc2",
)

stc2_bodega_inventario = table(
    "az_bodega_inventario",
    column("agroquimico_id"),
    column("az_catalogo_bodegas_id"),
    column("cantidad_disponible"),
    column("precio"),
    column("precio_asociado"),
    schema="stc2",
)


async def resolver_productos_comerciales(
    session: AsyncSession,
    principio_activo: str,
    sucursal_id: int,
    limite: int = 5,
) -> List[ProductoComercial]:
    """
    Asynchronously queries `stc2.agroquimico` joined with `stc2.az_bodega_inventario`
    to resolve available commercial SKUs for a technical active ingredient.

    GOVERNANCE SAFETY GATE (RSK-NEW-04):
    - Strictly filters for `cantidad_disponible > 0` (no out-of-stock suggestions).
    - Validates `estado == 1` (Active SKU) and `disponible_propuesta == 1`.
    - Returns an empty list if any database or connection error occurs, ensuring
      the technical advisory response never crashes due to catalog outages.

    Time Complexity: O(log N + K) where N is index size on `az_catalogo_bodegas_id`
    and K is the limit of returned rows.
    """
    if not principio_activo or not principio_activo.strip():
        logger.warning("Catalog resolver called with empty principio_activo.")
        return []

    # Clean molecule string for case-insensitive LIKE matching
    molecule_pattern = f"%{principio_activo.strip().lower()}%"

    # Construct non-blocking SQLAlchemy 2.0 Core SELECT statement
    query = (
        select(
            stc2_agroquimico.c.id.label("producto_id"),
            stc2_agroquimico.c.nombre.label("nombre_comercial"),
            stc2_agroquimico.c.ingrediente_activo.label("ingrediente_activo"),
            stc2_bodega_inventario.c.az_catalogo_bodegas_id.label("bodega_id"),
            stc2_bodega_inventario.c.cantidad_disponible,
            stc2_bodega_inventario.c.precio.label("precio_publico"),
            stc2_bodega_inventario.c.precio_asociado,
        )
        .select_from(
            stc2_agroquimico.join(
                stc2_bodega_inventario,
                stc2_agroquimico.c.id == stc2_bodega_inventario.c.agroquimico_id,
            )
        )
        .where(
            and_(
                stc2_agroquimico.c.estado == 1,
                stc2_agroquimico.c.disponible_propuesta == 1,
                stc2_bodega_inventario.c.az_catalogo_bodegas_id == sucursal_id,
                stc2_bodega_inventario.c.cantidad_disponible > 0,
                stc2_agroquimico.c.ingrediente_activo.ilike(molecule_pattern),
            )
        )
        # Order by lowest price first to favor cost-leadership for cooperative members
        .order_by(asc(stc2_bodega_inventario.c.precio))
        .limit(limite)
    )

    try:
        # AWAIT NON-BLOCKING I/O:
        # Yields control of the Python thread until PostgreSQL responds over TCP.
        result = await session.execute(query)
        rows = result.mappings().all()

        productos: List[ProductoComercial] = []
        for row in rows:
            productos.append(
                ProductoComercial(
                    producto_id=int(row["producto_id"]),
                    nombre_comercial=str(row["nombre_comercial"]),
                    ingrediente_activo=str(row["ingrediente_activo"] or principio_activo),
                    bodega_id=int(row["bodega_id"]),
                    cantidad_disponible=int(row["cantidad_disponible"]),
                    precio_publico=float(row["precio_publico"] or 0.0),
                    precio_asociado=float(row["precio_asociado"] or 0.0),
                )
            )

        logger.info(
            f"Resolved {len(productos)} commercial SKUs for molecule '{principio_activo}' "
            f"at branch {sucursal_id}."
        )
        return productos

    except Exception as exc:
        # CIRCUIT BREAKER PATTERN:
        # Trap database failures so the parent RAG advisory can still return
        # agronomic text without a sales suggestion payload.
        logger.error(
            f"Database error resolving commercial catalog in stc2: {str(exc)}",
            exc_info=True,
        )
        return []


def construir_carrito_items(
    productos: List[ProductoComercial],
    requiere_validacion_tecnica: bool = False,
    cantidad_default: int = 1,
) -> List[CarritoItem]:
    """
    Transforms resolved commercial SKUs into actionable App AldeaZON cart items.

    SAFETY GATE (RSK-NEW-04):
    If the technical query escalated to HITL or abstained, this function MUST NOT
    be called by the controller, or must be passed an empty list to guarantee
    a strictly empty `carrito: []` payload.
    """
    carrito: List[CarritoItem] = []
    for prod in productos:
        # Favor discounted cooperative member pricing if configured (> 0.0),
        # otherwise fall back to standard public retail price.
        precio_aplicable = (
            prod.precio_asociado
            if prod.precio_asociado > 0.0
            else prod.precio_publico
        )

        carrito.append(
            CarritoItem(
                sku_id=prod.producto_id,
                nombre=prod.nombre_comercial,
                cantidad_sugerida=cantidad_default,
                precio_unitario_usd=precio_aplicable,
                bodega_origen_id=prod.bodega_id,
                requiere_validacion_tecnica=requiere_validacion_tecnica,
            )
        )

    return carrito