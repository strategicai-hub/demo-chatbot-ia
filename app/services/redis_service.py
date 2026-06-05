import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import redis.asyncio as redis

from app.config import settings
from app.services import redis_keys as keys

_pool: redis.Redis | None = None


async def get_redis() -> redis.Redis:
    global _pool
    if _pool is None:
        _pool = redis.from_url(settings.redis_url, decode_responses=True)
    return _pool


def _block_ttl_seconds() -> int:
    # Bloqueio expira amanhã às 08:00 SP — bot só volta no dia seguinte.
    tz = ZoneInfo("America/Sao_Paulo")
    now = datetime.now(tz)
    target = (now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    return max(int((target - now).total_seconds()), 60)


# --------------- bloqueio de agente ---------------

async def set_block(phone: str, ttl: int | None = None, reason: str = "human") -> None:
    r = await get_redis()
    await r.set(keys.block_key(phone), reason or "human", ex=ttl or _block_ttl_seconds())


async def is_blocked(phone: str) -> bool:
    r = await get_redis()
    return await r.exists(keys.block_key(phone)) == 1



async def clear_stale_legacy_block(phone: str) -> bool:
    """Remove bloqueio antigo deixado por eco do /reset.

    Versoes anteriores gravavam o bloqueio como "1". Se o reset apagou lead,
    historico e buffer, mas um eco outbound criou esse bloqueio legado logo
    depois, a proxima mensagem do lead nao pode ficar presa ate o dia seguinte.
    """
    r = await get_redis()
    block_key = keys.block_key(phone)
    value = await r.get(block_key)
    if value != "1":
        return False

    has_history = await r.llen(keys.history_key(phone)) > 0
    has_lead = await r.exists(keys.lead_key(phone)) == 1
    has_buffer = await r.exists(keys.buffer_key(phone)) == 1
    if has_history or has_lead or has_buffer:
        return False

    await r.delete(block_key)
    return True


async def delete_block(phone: str) -> None:
    r = await get_redis()
    await r.delete(keys.block_key(phone))


# --------------- eco de mensagens enviadas pelo bot ---------------

def _normalize_outbound_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


async def mark_bot_outbound(phone: str, text: str = "", ttl: int = 300) -> None:
    r = await get_redis()
    await r.set(keys.bot_outbound_key(phone), "1", ex=ttl)
    normalized = _normalize_outbound_text(text)
    if normalized:
        await r.rpush(keys.bot_outbound_texts_key(phone), normalized)
        await r.ltrim(keys.bot_outbound_texts_key(phone), -20, -1)
        await r.expire(keys.bot_outbound_texts_key(phone), ttl)


async def is_bot_outbound(phone: str, text: str = "") -> bool:
    r = await get_redis()
    normalized = _normalize_outbound_text(text)
    if not normalized:
        return await r.exists(keys.bot_outbound_key(phone)) == 1

    try:
        recent_texts = await r.lrange(keys.bot_outbound_texts_key(phone), 0, -1)
    except redis.ResponseError:
        recent_texts = []
    return normalized in {_normalize_outbound_text(item) for item in recent_texts}


# --------------- idempotencia por mensagem (anti-duplicata) ---------------

async def mark_message_processed(fingerprint: str, ttl: int = 600) -> bool:
    """Marca uma mensagem como processada de forma atomica.

    Retorna True se foi a PRIMEIRA vez (deve processar) ou False se essa
    mensagem ja tinha sido vista (webhook duplicado -> descartar). Usa SET NX,
    entao nao ha corrida mesmo com duas entregas quase simultaneas.
    """
    if not fingerprint:
        return True
    r = await get_redis()
    created = await r.set(keys.dedup_key(fingerprint), "1", nx=True, ex=ttl)
    return bool(created)


# --------------- buffer de mensagens (debounce) ---------------

async def push_buffer(phone: str, text: str) -> int:
    r = await get_redis()
    return await r.rpush(keys.buffer_key(phone), text)


async def get_buffer(phone: str) -> list[str]:
    r = await get_redis()
    return await r.lrange(keys.buffer_key(phone), 0, -1)


async def delete_buffer(phone: str) -> None:
    r = await get_redis()
    await r.delete(keys.buffer_key(phone))


# --------------- historico de chat (Gemini) ---------------

async def get_chat_history(phone: str) -> list[dict]:
    r = await get_redis()
    raw = await r.lrange(keys.history_key(phone), 0, -1)
    history = []
    for item in raw:
        entry = json.loads(item)
        if "type" in entry:
            # Formato novo: {"type": "ai"/"human", "data": {"content": "..."}}
            role = "model" if entry["type"] == "ai" else "user"
            text = entry.get("data", {}).get("content", "")
            history.append({"role": role, "parts": [{"text": text}]})
        else:
            # Formato legado: passa direto para o Gemini
            history.append(entry)
    return history


async def append_chat_history(phone: str, role: str, text: str) -> None:
    r = await get_redis()
    entry_type = "ai" if role == "model" else "human"
    entry = json.dumps({"type": entry_type, "data": {"content": text}}, ensure_ascii=False)
    await r.rpush(keys.history_key(phone), entry)
    await r.ltrim(keys.history_key(phone), -50, -1)  # manter ultimas 50 msgs


async def clear_chat_history(phone: str) -> None:
    r = await get_redis()
    await r.delete(keys.history_key(phone))


# --------------- alerta de atendimento humano ---------------

async def set_alert_sent(phone: str, ttl: int = 3600) -> None:
    r = await get_redis()
    await r.set(keys.alert_key(phone), "1", ex=ttl)


async def is_alert_sent(phone: str) -> bool:
    r = await get_redis()
    return await r.exists(keys.alert_key(phone)) == 1


# --------------- convite presencial (idempotencia) ---------------

async def is_invite_sent(phone: str) -> bool:
    r = await get_redis()
    return await r.exists(keys.invite_sent_key(phone)) == 1


async def set_invite_sent(phone: str, ttl: int = 60 * 60 * 24 * 30) -> None:
    r = await get_redis()
    await r.set(keys.invite_sent_key(phone), "1", ex=ttl)


async def clear_invite_sent(phone: str) -> None:
    r = await get_redis()
    await r.delete(keys.invite_sent_key(phone))


# --------------- leads ---------------

async def get_lead(phone: str) -> dict | None:
    r = await get_redis()
    data = await r.hgetall(keys.lead_key(phone))
    return data if data else None


async def create_lead(phone: str, name: str = "") -> dict:
    r = await get_redis()
    lead = {
        "phone": phone,
        "name": name,
        "status_conversa": "Novo",
        "created_at": "",
    }
    await r.hset(keys.lead_key(phone), mapping=lead)
    return lead


async def update_lead(phone: str, **fields) -> None:
    r = await get_redis()
    if fields:
        await r.hset(keys.lead_key(phone), mapping=fields)


async def delete_lead(phone: str) -> None:
    r = await get_redis()
    await r.delete(keys.lead_key(phone))
