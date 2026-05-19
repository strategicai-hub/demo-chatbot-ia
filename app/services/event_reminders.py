from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import hashlib
import logging
import re
from zoneinfo import ZoneInfo

from app.client_data import load_client_data
from app.config import settings
from app.services import redis_keys as keys
from app.services import uazapi
from app.services.redis_service import append_chat_history, get_redis, update_lead

logger = logging.getLogger(__name__)

EVENT_NICHE = "lancamento_livro"
_SP_TZ = ZoneInfo("America/Sao_Paulo")

_REMINDER_SENT_FIELDS = {
    "day_before": "reminder_day_before_sent_at",
    "confirmation": "reminder_confirmation_sent_at",
    "final": "reminder_final_sent_at",
    "post_event_survey": "reminder_post_event_survey_sent_at",
}


def _fold_text(text: str) -> str:
    import unicodedata

    normalized = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def normalize_phone(raw: str) -> str:
    """Normaliza telefone brasileiro para digitos com DDI, sem sinal de +."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) in (10, 11):
        digits = "55" + digits
    if not (12 <= len(digits) <= 13):
        raise ValueError("Telefone invalido. Informe DDD + numero.")
    return digits


def event_data() -> dict:
    return load_client_data(niche=EVENT_NICHE)


def registration_messages() -> tuple[str, str]:
    event = event_data().get("event") or {}
    return event.get("first_message", ""), event.get("second_message", "")


def event_id() -> str:
    event = event_data().get("event") or {}
    return event.get("id") or "comunicacao-humanizada"


def _parse_window(date_value: str, time_value: str) -> datetime:
    return datetime.fromisoformat(f"{date_value}T{time_value}:00").replace(tzinfo=_SP_TZ)


def scheduled_at(phone: str, reminder_name: str, reminder_cfg: dict) -> datetime:
    start = _parse_window(reminder_cfg["date"], reminder_cfg["start"])
    end = _parse_window(reminder_cfg["date"], reminder_cfg["end"])
    span_minutes = max(int((end - start).total_seconds() // 60), 0)
    if span_minutes == 0:
        return start
    digest = hashlib.sha256(f"{phone}:{reminder_name}".encode("utf-8")).hexdigest()
    offset_minutes = int(digest[:8], 16) % (span_minutes + 1)
    return start + timedelta(minutes=offset_minutes)


def window_end(reminder_cfg: dict) -> datetime:
    return _parse_window(reminder_cfg["date"], reminder_cfg["end"])


def access_or_arrival_text(data: dict) -> str:
    event = data.get("event") or {}
    access_link = (event.get("access_link") or "").strip()
    arrival = (event.get("arrival_instructions") or "").strip()

    parts = []
    if access_link:
        parts.append(f"Link de acesso: {access_link}")
    if arrival:
        parts.append(arrival)
    if not parts:
        parts.append("Se precisar, responda aqui que te ajudo com as orientações finais.")
    return " ".join(parts)


def format_reminder_message(reminder_name: str, lead: dict, data: dict) -> str:
    reminder = ((data.get("reminders") or {}).get(reminder_name) or {})
    template = reminder.get("message") or ""
    raw_name = (lead.get("name") or "").strip()
    first_name = raw_name.split()[0] if raw_name else "tudo bem"
    return template.format(
        name=first_name,
        access_or_arrival=access_or_arrival_text(data),
    ).strip()


def parse_presence_confirmation(text: str) -> str | None:
    t = _fold_text(text or "").strip()
    if not t:
        return None

    negative_markers = (
        "nao vou",
        "nao poderei",
        "nao posso",
        "nao consigo",
        "nao estarei",
        "cancelar",
        "cancela",
        "infelizmente nao",
    )
    if t in {"nao", "n"} or any(marker in t for marker in negative_markers):
        return "não"

    uncertain_markers = ("nao sei", "talvez", "vou ver", "depende")
    if any(marker in t for marker in uncertain_markers):
        return None

    positive_markers = (
        "sim",
        "confirmo",
        "confirmado",
        "pode confirmar",
        "vou participar",
        "eu vou",
        "estarei",
        "presenca confirmada",
    )
    if t in {"sim", "s"} or any(marker in t for marker in positive_markers):
        return "sim"

    return None


def parse_post_event_survey(text: str) -> tuple[str | None, str]:
    """Extrai nota 0-10 e maior aprendizado de uma resposta livre."""
    cleaned = (text or "").strip()
    if not cleaned:
        return None, ""

    score: str | None = None
    score_match = None
    score_patterns = (
        r"(?:^|\n)\s*1[\).]\s*(10|[0-9])(?!\d)",
        r"(?:nota|score|dou|daria|percepção|percepcao)[^\d]*(10|[0-9])(?!\d)",
        r"(?<!\d)(10|[0-9])(?![\d\)])",
    )
    for pattern in score_patterns:
        score_match = re.search(pattern, cleaned, flags=re.IGNORECASE)
        if score_match:
            score = score_match.group(1)
            break

    learning = cleaned
    learning_patterns = (
        r"(?:2\)|2\.|segund[ao]\s+pergunta[:\-\s]*)(.+)$",
        r"(?:maior aprendizado(?:\s+foi)?[:\-\s]*)(.+)$",
        r"(?:aprendizado[:\-\s]*)(.+)$",
    )
    for pattern in learning_patterns:
        match = re.search(pattern, cleaned, flags=re.IGNORECASE | re.DOTALL)
        if match:
            learning = match.group(1).strip()
            break

    if score_match and learning == cleaned:
        learning = (cleaned[: score_match.start()] + cleaned[score_match.end() :]).strip(" .,:;-\n")

    learning = re.sub(r"^\s*(?:1\)|1\.|2\)|2\.)\s*", "", learning).strip()
    return score, learning


async def update_confirmation_from_message(phone: str, lead: dict, text: str) -> str | None:
    if (lead or {}).get("nicho") != EVENT_NICHE:
        return None
    confirmed = parse_presence_confirmation(text)
    if not confirmed:
        return None
    await update_lead(
        phone,
        confirmado=confirmed,
        confirmed_at=datetime.now(_SP_TZ).isoformat(timespec="seconds"),
    )
    return confirmed


async def update_post_event_survey_from_message(phone: str, lead: dict, text: str) -> tuple[str | None, str] | None:
    if (lead or {}).get("nicho") != EVENT_NICHE:
        return None
    if not (lead.get("reminder_post_event_survey_sent_at") or lead.get("last_reminder") == "post_event_survey"):
        return None

    score, learning = parse_post_event_survey(text)
    if score is None and not learning:
        return None

    fields = {"survey_answered_at": datetime.now(_SP_TZ).isoformat(timespec="seconds")}
    if score is not None:
        fields["survey_score"] = score
    if learning:
        fields["survey_learning"] = learning[:1000]
    await update_lead(phone, **fields)
    return score, learning


def _should_skip_reminder(reminder_name: str, lead: dict) -> bool:
    if reminder_name == "final" and lead.get("confirmado") == "não":
        return True
    if reminder_name == "post_event_survey" and lead.get("confirmado") == "não":
        return True
    return False


async def send_due_reminders(now: datetime | None = None) -> int:
    if not settings.EVENT_REMINDERS_ENABLED:
        return 0

    now = now or datetime.now(_SP_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_SP_TZ)

    data = event_data()
    reminder_cfgs = data.get("reminders") or {}
    if not reminder_cfgs:
        return 0

    r = await get_redis()
    lead_keys = await r.keys(keys.lead_scan_pattern())
    sent_count = 0

    for lead_key in lead_keys:
        lead = await r.hgetall(lead_key)
        if lead.get("nicho") != EVENT_NICHE:
            continue
        phone = keys.phone_from_lead_key(lead_key)

        for reminder_name, sent_field in _REMINDER_SENT_FIELDS.items():
            reminder_cfg = reminder_cfgs.get(reminder_name) or {}
            if not reminder_cfg or lead.get(sent_field):
                continue
            if _should_skip_reminder(reminder_name, lead):
                continue

            due_at = scheduled_at(phone, reminder_name, reminder_cfg)
            if now < due_at:
                continue
            if now > window_end(reminder_cfg) + timedelta(minutes=5):
                continue

            message = format_reminder_message(reminder_name, lead, data)
            if not message:
                continue

            try:
                await uazapi.send_text(phone, message)
                await append_chat_history(phone, "model", message)
                await update_lead(
                    phone,
                    **{
                        sent_field: now.isoformat(timespec="seconds"),
                        "last_reminder": reminder_name,
                    },
                )
                sent_count += 1
                logger.info("Lembrete %s enviado para %s", reminder_name, phone)
            except Exception:
                logger.exception("Erro ao enviar lembrete %s para %s", reminder_name, phone)

    return sent_count


async def start_loop() -> None:
    while True:
        try:
            await send_due_reminders()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Erro no loop de lembretes do evento")
        await asyncio.sleep(max(settings.EVENT_REMINDER_POLL_SECONDS, 10))
