"""
Edita una clave de atv.prompt_config con la MISMA validación que usa la API
(app/services/prompt_config.construir_prompt_config): una plantilla sin sus
placeholders obligatorios, un número inválido o un texto vacío se rechazan
antes de guardar, así un error de edición no deja la API respondiendo 503.

Lo usa la consola de pruebas (tools/chat_streamlit, pestaña "Prompts del
agente"), que corre en otro entorno virtual sin SQLAlchemy:

    echo '{"clave": "...", "valor": "..."}' | python -m scripts.editar_prompt_config validar
    echo '{"clave": "...", "valor": "...", "autor": "...", "motivo": "..."}' \
        | python -m scripts.editar_prompt_config guardar

Responde una línea JSON: {"ok": true, ...} o {"ok": false, "error": "..."}.
`guardar` valida y actualiza en una sola transacción (con la fila bloqueada) y
deja el valor anterior en atv.prompt_config_historial (scripts/016_*.sql).
La API lee atv.prompt_config en cada consulta: el cambio rige desde la siguiente.
"""

import asyncio
import json
import sys

from sqlalchemy import text

from app.db.session import AsyncSessionLocal
from app.services.prompt_config import PromptConfigError, construir_prompt_config

MAX_AUTOR = 64


async def _filas_activas(db) -> dict:
    return {c: v for c, v in (await db.execute(
        text("SELECT clave, valor FROM atv.prompt_config WHERE activo"))).all()}


async def validar(clave: str, valor: str) -> dict:
    async with AsyncSessionLocal() as db:
        filas = await _filas_activas(db)
    if clave not in filas:
        return {"ok": False, "error": f"'{clave}' no existe o no está activa en atv.prompt_config"}
    try:
        construir_prompt_config(filas | {clave: valor})
    except PromptConfigError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "sin_cambios": filas[clave] == valor}


async def guardar(clave: str, valor: str, autor: str, motivo: str | None) -> dict:
    async with AsyncSessionLocal() as db:
        anterior = (await db.execute(
            text("SELECT valor FROM atv.prompt_config WHERE clave = :c AND activo FOR UPDATE"), {"c": clave})).scalar()
        if anterior is None:
            return {"ok": False, "error": f"'{clave}' no existe o no está activa en atv.prompt_config"}
        if anterior == valor:
            return {"ok": True, "sin_cambios": True}
        try:
            construir_prompt_config(await _filas_activas(db) | {clave: valor})
        except PromptConfigError as exc:
            await db.rollback()
            return {"ok": False, "error": str(exc)}
        await db.execute(text("""
            INSERT INTO atv.prompt_config_historial (clave, valor_anterior, valor_nuevo, autor, motivo)
            VALUES (:c, :anterior, :valor, :autor, :motivo)"""),
            {"c": clave, "anterior": anterior, "valor": valor, "autor": autor, "motivo": motivo})
        await db.execute(text("""
            UPDATE atv.prompt_config SET valor = :valor, ts_actualizado = CURRENT_TIMESTAMP, actualizado_por = :autor
             WHERE clave = :c"""), {"c": clave, "valor": valor, "autor": autor})
        await db.commit()
    return {"ok": True, "sin_cambios": False}


def main() -> None:
    accion = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        datos = json.loads(sys.stdin.read())
        clave, valor = str(datos["clave"]), str(datos["valor"])
        if accion == "validar":
            resultado = asyncio.run(validar(clave, valor))
        elif accion == "guardar":
            autor = str(datos.get("autor") or "consola").strip()[:MAX_AUTOR]
            resultado = asyncio.run(guardar(clave, valor, autor, datos.get("motivo") or None))
        else:
            resultado = {"ok": False, "error": "acción inválida: use 'validar' o 'guardar'"}
    except (KeyError, ValueError) as exc:
        resultado = {"ok": False, "error": f"entrada inválida: {exc}"}
    print(json.dumps(resultado, ensure_ascii=False))


if __name__ == "__main__":
    main()
