"""
Fonte unica de verdade para nomes de chaves do Redis deste projeto.

Schema:
    <phone>--<slug>:<type>    -> dados por lead (buffer, lead, history, block, alert)
    <slug>:logs               -> lista global de logs de execucao
    <slug>:*                  -> qualquer outra chave global do projeto
"""
from app.config import settings


def _phone_ns(phone: str) -> str:
    return f"{phone}--{settings.PROJECT_SLUG}"


def buffer_key(phone: str) -> str:
    return f"{_phone_ns(phone)}:buffer"


def lead_key(phone: str) -> str:
    return f"{_phone_ns(phone)}:lead"


def history_key(phone: str) -> str:
    return f"{_phone_ns(phone)}:history"


def block_key(phone: str) -> str:
    return f"{_phone_ns(phone)}:block"


def bot_outbound_key(phone: str) -> str:
    return f"{_phone_ns(phone)}:bot_outbound"


def bot_outbound_texts_key(phone: str) -> str:
    return f"{_phone_ns(phone)}:bot_outbound:texts"


def alert_key(phone: str) -> str:
    return f"{_phone_ns(phone)}:alert"


def invite_sent_key(phone: str) -> str:
    return f"{_phone_ns(phone)}:invite_sent"


def followup_active_key(phone: str) -> str:
    return f"{_phone_ns(phone)}:followup:active"


def session_log_key() -> str:
    return f"{settings.PROJECT_SLUG}:logs"


# --- campanha de outreach (disparo proativo) ---

def outreach_daily_key(yyyymmdd: str) -> str:
    return f"{settings.PROJECT_SLUG}:outreach:count:{yyyymmdd}"


def outreach_campaign_start_key() -> str:
    return f"{settings.PROJECT_SLUG}:outreach:campaign_start"


# --- patterns + helpers para as rotas de leitura ---

def lead_scan_pattern() -> str:
    return f"*--{settings.PROJECT_SLUG}:lead"


def history_scan_pattern() -> str:
    return f"*--{settings.PROJECT_SLUG}:history"


def phone_from_lead_key(key: str) -> str:
    suffix = f"--{settings.PROJECT_SLUG}:lead"
    return key[: -len(suffix)] if key.endswith(suffix) else key


def phone_from_history_key(key: str) -> str:
    suffix = f"--{settings.PROJECT_SLUG}:history"
    return key[: -len(suffix)] if key.endswith(suffix) else key
