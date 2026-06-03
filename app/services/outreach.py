"""Campanha de outreach: disparo proativo "vai ao evento presencial?".

Fluxo:
  1. A LP (lp_c_humanizada) e a fonte de verdade da fila e do liga/desliga.
     Botao no painel admin seta a flag `outreach_active`.
  2. Este loop puxa a fila (`/api/outreach/pending`) e envia a 1a mensagem com
     RITMO ANTI-BAN: janela horaria, intervalo aleatorio, ramp-up diario e
     pausas longas. Cada envio e reportado de volta (`/api/outreach/status`),
     que alimenta a coluna de status do painel.
  3. A RESPOSTA do lead e tratada deterministicamente por `handle_reply`
     (chamado pelo consumer): confirma presenca -> pede o nome do convite ->
     gera/envia o convite com QR (reaproveitando o fluxo do consumer).

Anti-ban (por que cada peca existe):
  - intervalo ALEATORIO (nao fixo): padrao de bot e justamente o intervalo fixo.
  - ramp-up diario: numero "frio" disparando em massa e o gatilho classico.
  - janela horaria: disparo de madrugada levanta flag.
  - spintax: texto identico repetido e o sinal #1 de spam.
  - so 1 mensagem por lead (sem follow-up): menos chance de report.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings
from app.services import event_reminders
from app.services import invitation
from app.services import redis_keys as keys
from app.services import uazapi
from app.services.redis_service import (
    append_chat_history,
    get_lead,
    get_redis,
    is_blocked,
    is_invite_sent,
    update_lead,
)

logger = logging.getLogger(__name__)
_SP_TZ = ZoneInfo("America/Sao_Paulo")


# --------------- spintax ---------------

_SPINTAX_RE = re.compile(r"\{([^{}]*)\}")


def expand_spintax(template: str) -> str:
    """Expande {a|b|c} escolhendo uma opcao aleatoria (resolve aninhados)."""
    text = template
    for _ in range(6):
        new = _SPINTAX_RE.sub(lambda m: random.choice(m.group(1).split("|")), text)
        if new == text:
            break
        text = new
    return text


# Os textos abaixo sao DEFAULTS. O texto real vem de `client.lancamento_livro.yaml`
# (bloco `outreach:`), editavel sem mexer em codigo. Estes constantes so entram
# se a chave estiver ausente no yaml.
# IMPORTANTE: {name} e injetado ANTES da expansao do spintax (nao tem '|', entao
# seria consumido pelo regex e perderia as chaves). Por isso build_opener
# substitui {name} primeiro.
_DEFAULTS = {
    "opener": (
        "{Oi|Olá|Opa}, {name}! {Aqui é a|Quem fala é a} Mya, do lançamento do livro "
        "Comunicação Humanizada 🙌\n\n"
        "{Vi que você se inscreveu|Você garantiu sua inscrição} e "
        "{queria confirmar|passei pra confirmar} uma coisinha: você "
        "{pretende ir|vai} ao evento *presencial* {nesta sexta, 05/06|no dia 05/06}?"
    ),
    "name_request": (
        "{Que bom|Maravilha|Perfeito} que você vai! 🎉 "
        "Para eu gerar o seu convite com QR Code de check-in, "
        "{qual nome você quer que apareça nele|qual nome devo colocar no convite}?"
    ),
    "decline_reply": (
        "{Sem problema|Tudo bem|Sem crise}! Obrigada por avisar 💛 "
        "Se mudar de ideia ou preferir participar online, é só me chamar por aqui."
    ),
    "incomplete_name_reply": (
        "Pode me mandar seu nome completo (nome e sobrenome) pra eu colocar no convite?"
    ),
    "after_invite_closing": (
        "{Prontinho|Pronto}! Te espero no dia 05/06, às 18:30 😊 "
        "Qualquer dúvida, é só me chamar por aqui."
    ),
}


def _template(key: str) -> str:
    """Le o template do yaml (bloco `outreach:`), com fallback para o default."""
    cfg = event_reminders.event_data().get("outreach") or {}
    raw = cfg.get(key)
    return raw.strip() if isinstance(raw, str) and raw.strip() else _DEFAULTS[key]


def _first_name(name: str) -> str:
    raw = (name or "").strip()
    return raw.split()[0] if raw else "tudo bem"


def build_opener(name: str) -> str:
    text = _template("opener").replace("{name}", _first_name(name))
    return expand_spintax(text)


def build_name_request() -> str:
    return expand_spintax(_template("name_request"))


def build_decline_reply() -> str:
    return expand_spintax(_template("decline_reply"))


def build_incomplete_name_reply() -> str:
    return expand_spintax(_template("incomplete_name_reply"))


def build_after_invite_closing() -> str:
    return expand_spintax(_template("after_invite_closing"))


# --------------- seeding do lead ---------------

async def _ensure_outreach_lead(phone: str, name: str) -> dict:
    """Cria/atualiza o lead no Redis e trava o nicho do evento (igual ao welcome)."""
    now = datetime.now(_SP_TZ).isoformat(timespec="seconds")
    lead = await get_lead(phone)
    if not lead:
        from app.services.redis_service import create_lead

        await create_lead(phone, name)
        lead = await get_lead(phone) or {}

    fields = {
        "nicho": event_reminders.EVENT_NICHE,
        "source": "formulario_evento",
        "event_id": event_reminders.event_id(),
        "atualizado_em": now,
    }
    if name and not lead.get("name"):
        fields["name"] = name
    if not lead.get("inscrito_em"):
        fields["inscrito_em"] = now
    await update_lead(phone, **fields)
    lead.update(fields)
    return lead


# --------------- ramp-up / cap diario ---------------

async def _daily_cap(now: datetime) -> int:
    ramp = settings.outreach_daily_ramp
    r = await get_redis()
    start = await r.get(keys.outreach_campaign_start_key())
    if not start:
        return ramp[0]
    try:
        start_dt = datetime.strptime(start, "%Y%m%d")
        today_dt = datetime.strptime(now.strftime("%Y%m%d"), "%Y%m%d")
        idx = max((today_dt - start_dt).days, 0)
    except Exception:
        idx = 0
    return ramp[min(idx, len(ramp) - 1)]


async def _today_count(now: datetime) -> int:
    r = await get_redis()
    val = await r.get(keys.outreach_daily_key(now.strftime("%Y%m%d")))
    return int(val) if val else 0


async def _bump_count(now: datetime) -> None:
    r = await get_redis()
    today = now.strftime("%Y%m%d")
    k = keys.outreach_daily_key(today)
    val = await r.incr(k)
    if val == 1:
        await r.expire(k, 60 * 60 * 48)
    await r.setnx(keys.outreach_campaign_start_key(), today)


# --------------- envio da abertura ---------------

async def _send_opener(phone: str, name: str) -> bool:
    try:
        await _ensure_outreach_lead(phone, name)
        text = build_opener(name)
        await uazapi.send_text(phone, text)
        await append_chat_history(phone, "model", text)
        await update_lead(
            phone,
            outreach_stage="asked",
            outreach_sent_at=datetime.now(_SP_TZ).isoformat(timespec="seconds"),
        )
        await invitation.report_outreach_status(phone, "sent")
        logger.info("Outreach: abertura enviada para %s", phone)
        return True
    except Exception as exc:
        logger.exception("Outreach: falha ao enviar abertura para %s", phone)
        await invitation.report_outreach_status(phone, "failed", str(exc))
        return False


def _safe_phone(raw: str) -> str:
    try:
        return event_reminders.normalize_phone(raw or "")
    except Exception:
        return ""


# --------------- loop principal ---------------

async def start_loop() -> None:
    if not settings.OUTREACH_ENABLED:
        logger.info("Campanha de outreach desligada (OUTREACH_ENABLED=false)")
        return

    poll = max(settings.OUTREACH_POLL_SECONDS, 10)
    sent_since_pause = 0
    logger.info("Loop de outreach iniciado")

    while True:
        try:
            now = datetime.now(_SP_TZ)

            # 1) fora da janela horaria -> espera
            if not (settings.OUTREACH_WINDOW_START <= now.hour < settings.OUTREACH_WINDOW_END):
                await asyncio.sleep(min(poll, 600))
                continue

            # 2) campanha ativa? (a LP devolve active + fila)
            try:
                data = await invitation.fetch_pending(limit=25)
            except Exception as exc:
                logger.warning("Outreach: falha ao buscar fila na LP: %s", exc)
                await asyncio.sleep(poll)
                continue
            if not data.get("active"):
                await asyncio.sleep(poll)
                continue

            # 3) cap diario (ramp-up)
            cap = await _daily_cap(now)
            count = await _today_count(now)
            if count >= cap:
                logger.info("Outreach: cap diario atingido (%d/%d) - aguardando", count, cap)
                await asyncio.sleep(min(poll, 600))
                continue

            # 4) proximo lead elegivel
            target = None
            for row in (data.get("leads") or []):
                phone = _safe_phone(row.get("phone", ""))
                if not phone:
                    continue
                if await is_blocked(phone):
                    continue
                if await is_invite_sent(phone):
                    continue
                existing = await get_lead(phone)
                if existing and existing.get("outreach_stage"):
                    continue  # ja perguntado nesta instancia
                target = (phone, row.get("name", ""))
                break

            if target is None:
                await asyncio.sleep(poll)
                continue

            phone, name = target
            ok = await _send_opener(phone, name)
            if ok:
                await _bump_count(now)
                sent_since_pause += 1

            # 5) ritmo: pausa longa periodica OU intervalo aleatorio
            if sent_since_pause >= max(settings.OUTREACH_PAUSE_EVERY, 1):
                sent_since_pause = 0
                pause = random.randint(settings.OUTREACH_PAUSE_MIN, settings.OUTREACH_PAUSE_MAX)
                logger.info("Outreach: pausa longa de %ds apos %d envios", pause, settings.OUTREACH_PAUSE_EVERY)
                await asyncio.sleep(pause)
            else:
                interval = random.randint(settings.OUTREACH_MIN_INTERVAL, settings.OUTREACH_MAX_INTERVAL)
                await asyncio.sleep(interval)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Erro no loop de outreach")
            await asyncio.sleep(30)


# --------------- tratamento da resposta (chamado pelo consumer) ---------------

async def handle_reply(phone: str, lead: dict, text: str) -> bool:
    """Trata a resposta de um lead que recebeu a abertura da campanha.

    Retorna True se a mensagem foi tratada aqui (o consumer NAO chama o Gemini).
    Retorna False para respostas ambiguas/perguntas -> caem no fluxo normal.
    """
    stage = (lead or {}).get("outreach_stage")
    if stage not in ("asked", "awaiting_name"):
        return False

    answer = (text or "").strip()
    if not answer:
        return False

    if stage == "asked":
        presence = event_reminders.parse_presence_confirmation(answer)
        if presence == "sim":
            await update_lead(phone, outreach_stage="awaiting_name", confirmado="sim", presenca="presencial")
            msg = build_name_request()
            await uazapi.send_text(phone, msg)
            await append_chat_history(phone, "user", answer)
            await append_chat_history(phone, "model", msg)
            return True
        if presence == "não":
            await update_lead(phone, outreach_stage="declined", confirmado="não")
            msg = build_decline_reply()
            await uazapi.send_text(phone, msg)
            await append_chat_history(phone, "user", answer)
            await append_chat_history(phone, "model", msg)
            return True
        # ambiguo / pergunta -> deixa o Gemini responder normalmente
        return False

    # stage == "awaiting_name"
    # se parece pergunta ou texto longo, devolve ao Gemini (pode ser duvida)
    if "?" in answer or len(answer) > 60:
        return False

    name = re.sub(r"\s+", " ", answer).strip(" .,:;-")
    if len(name.split()) < 2:
        msg = build_incomplete_name_reply()
        await uazapi.send_text(phone, msg)
        await append_chat_history(phone, "user", answer)
        await append_chat_history(phone, "model", msg)
        return True

    await update_lead(phone, name=name)
    lead["name"] = name
    await append_chat_history(phone, "user", answer)

    # gera + envia o convite reaproveitando o fluxo idempotente do consumer
    from app.consumer import _send_personalized_invite

    await _send_personalized_invite(phone, lead)
    await update_lead(phone, outreach_stage="done")

    closing = build_after_invite_closing()
    await uazapi.send_text(phone, closing)
    await append_chat_history(phone, "model", closing)
    return True
