# tests/test_catalog.py
import pytest
from unittest.mock import AsyncMock
from app.services.catalog import resolver_productos_comerciales, construir_carrito_items
from app.schemas.domain import ProductoComercial

@pytest.mark.asyncio
async def test_resolver_productos_comerciales_returns_empty_on_db_exception():
    """Ensure DB connectivity issues fail gracefully without crashing advisory serving."""
    mock_session = AsyncMock()
    mock_session.execute.side_effect = Exception("VPS1 MariaDB connection timeout")

    resultados = await resolver_productos_comerciales(
        session=mock_session,
        principio_activo="Imidacloprid",
        sucursal_id=101
    )

    assert resultados == []
    mock_session.execute.assert_called_once()

def test_construir_carrito_items_selects_preferential_member_price():
    """Ensure cooperative member price takes precedence over standard public price."""
    producto = ProductoComercial(
        producto_id=402,
        nombre_comercial="Consento 45 SC - 1 Litro",
        ingrediente_activo="Propamocarb",
        bodega_id=1,
        cantidad_disponible=15,
        precio_publico=28.00,
        precio_asociado=24.50
    )

    items = construir_carrito_items([producto], requiere_validacion_tecnica=False)
    
    assert len(items) == 1
    assert items[0].sku_id == 402
    assert items[0].precio_unitario_usd == 24.50

def test_construir_carrito_items_public_price_for_non_members():
    """Web users who are not verified active members pay the public price."""
    producto = ProductoComercial(
        producto_id=402,
        nombre_comercial="Consento 45 SC - 1 Litro",
        ingrediente_activo="Propamocarb",
        bodega_id=1,
        cantidad_disponible=15,
        precio_publico=28.00,
        precio_asociado=24.50
    )

    items = construir_carrito_items([producto], aplicar_precio_asociado=False)

    assert items[0].precio_unitario_usd == 28.00
