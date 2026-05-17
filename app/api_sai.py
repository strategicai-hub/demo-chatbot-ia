"""Endpoint que recebe o push do SAI Comercial com o snapshot do painel.

POST /sai/config
Headers: x-ingest-secret: <SAI_INGEST_SECRET>
Body: snapshot completo (ver sai-comercial/docs/painel-ia-sync.md)
"""
from __future__ import annotations

import hmac

from fastapi import APIRouter, Header, HTTPException, Request

from app.config import settings
from app.services import sai_sync

router = APIRouter(prefix="/sai")


@router.post("/config")
async def receive_config(
    request: Request,
    x_ingest_secret: str | None = Header(default=None, alias="x-ingest-secret"),
):
    expected = settings.SAI_INGEST_SECRET
    if not expected:
        raise HTTPException(status_code=503, detail="sync nao configurado")
    if not x_ingest_secret or not hmac.compare_digest(x_ingest_secret, expected):
        raise HTTPException(status_code=401, detail="invalid secret")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="payload invalido")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload deve ser objeto")
    slug = payload.get("tenantSlug")
    if slug != settings.SAI_TENANT_SLUG:
        raise HTTPException(status_code=400, detail="tenantSlug nao confere")
    await sai_sync.save_snapshot(payload)
    return {"ok": True}
