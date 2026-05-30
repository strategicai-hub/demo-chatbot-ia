"""Cliente para o endpoint de convite presencial da LP (lp_c_humanizada).

A LP e a fonte unica de verdade do convite: gera a imagem com nome + QR de
check-in, cacheia e serve numa URL publica. Aqui so chamamos:
  POST {INVITE_API_URL}/api/invite   -> {image_url, checkin_url, token, already_sent}
  POST {INVITE_API_URL}/api/invite/sent -> marca como enviado (idempotencia)

Protegido por shared-secret no header `x-invite-secret` (igual ao da LP).
"""
import json as _json
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        # Geracao da imagem + QR pode levar alguns segundos na primeira vez.
        _client = httpx.AsyncClient(timeout=45)
    return _client


def _headers() -> dict:
    return {
        "Content-Type": "application/json; charset=utf-8",
        "x-invite-secret": settings.INVITE_API_SECRET,
    }


def _json_body(payload: dict) -> bytes:
    return _json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _base_url() -> str:
    return settings.INVITE_API_URL.rstrip("/")


async def fetch_invitation(phone: str, name: str) -> dict:
    """POST /api/invite {phone, name} -> dict com image_url, checkin_url, token, already_sent.

    Idempotente do lado da LP. Levanta excecao em falha; o consumer trata e cai no fallback.
    """
    if not settings.INVITE_API_SECRET:
        raise RuntimeError("INVITE_API_SECRET nao configurado - convite desligado")

    url = f"{_base_url()}/api/invite"
    payload = {"phone": phone, "name": name}
    client = _get_client()
    resp = await client.post(url, content=_json_body(payload), headers=_headers())
    resp.raise_for_status()
    data = resp.json()
    image_url = (data or {}).get("image_url")
    if not image_url:
        raise ValueError(f"Resposta da LP sem image_url: {data!r}")
    logger.info("Convite gerado para %s (token=%s)", phone, (data or {}).get("token"))
    return data


async def mark_sent(phone: str) -> None:
    """Avisa a LP que o convite foi enviado (seta invite_sent_at). Best-effort."""
    if not settings.INVITE_API_SECRET:
        return
    url = f"{_base_url()}/api/invite/sent"
    client = _get_client()
    try:
        resp = await client.post(url, content=_json_body({"phone": phone}), headers=_headers())
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("Falha ao marcar convite enviado na LP para %s: %s", phone, exc)
