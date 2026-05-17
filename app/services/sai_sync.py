"""Sincronizacao do snapshot do Painel IA WhatsApp (SAI Comercial).

O SAI dispara `POST {externalUrl}/sai/config` a cada Save no painel (push
fire-and-forget). Independentemente do push, este modulo poleia o endpoint
publico `GET {SAI_BASE_URL}/api/ia/public/config/{SAI_TENANT_SLUG}` no
startup e a cada 15 minutos como fallback. O snapshot completo eh gravado
em Redis (chave `sai:config:{slug}`) e consumido pelo prompt builder.

Contrato em sai-comercial/docs/painel-ia-sync.md.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
import redis as redis_sync
from redis.asyncio import Redis

from app.config import settings
from app.services.redis_service import get_redis

log = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 15 * 60
HTTP_TIMEOUT_SECONDS = 10.0


def _key() -> str:
    return f"sai:config:{settings.SAI_TENANT_SLUG}"


def _configured() -> bool:
    return bool(settings.SAI_TENANT_SLUG and settings.SAI_INGEST_SECRET)


async def save_snapshot(snapshot: dict[str, Any]) -> None:
    r: Redis = await get_redis()
    await r.set(_key(), json.dumps(snapshot, ensure_ascii=False))
    log.info("sai_sync: snapshot atualizado (updatedAt=%s)", snapshot.get("updatedAt"))


async def load_snapshot() -> dict[str, Any] | None:
    if not _configured():
        return None
    r: Redis = await get_redis()
    raw = await r.get(_key())
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


_sync_client: redis_sync.Redis | None = None


def load_snapshot_sync() -> dict[str, Any] | None:
    """Versao sincrona usada pelo prompt builder (que eh sync)."""
    if not _configured():
        return None
    global _sync_client
    if _sync_client is None:
        try:
            _sync_client = redis_sync.Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
        except Exception as exc:
            log.warning("sai_sync: nao conectou no redis (sync): %s", exc)
            return None
    try:
        raw = _sync_client.get(_key())
    except Exception as exc:
        log.warning("sai_sync: get sync falhou: %s", exc)
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def fetch_from_sai() -> dict[str, Any] | None:
    if not _configured():
        return None
    url = f"{settings.SAI_BASE_URL.rstrip('/')}/api/ia/public/config/{settings.SAI_TENANT_SLUG}"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            res = await client.get(url, headers={"x-ingest-secret": settings.SAI_INGEST_SECRET})
            if res.status_code == 200:
                return res.json()
            log.warning("sai_sync: GET %s -> %s", url, res.status_code)
    except Exception as exc:
        log.warning("sai_sync: GET %s falhou: %s", url, exc)
    return None


async def sync_now() -> None:
    snap = await fetch_from_sai()
    if snap:
        await save_snapshot(snap)


async def start_polling() -> None:
    """Loop infinito, executado em background no lifespan do FastAPI."""
    if not _configured():
        log.info("sai_sync: SAI_TENANT_SLUG/SAI_INGEST_SECRET ausentes — polling desativado")
        return
    log.info("sai_sync: polling iniciado (intervalo=%ss, slug=%s)", POLL_INTERVAL_SECONDS, settings.SAI_TENANT_SLUG)
    await sync_now()
    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            await sync_now()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("sai_sync: erro no loop de polling: %s", exc)
