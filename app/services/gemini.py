"""Wrapper do Gemini usando o SDK `google-genai` (novo SDK oficial).

Decisões importantes (regra global de chatbots Python):
- Usa `google-genai`. Evitar `google-generativeai` (legado).
- `thinking_budget=0` em TODA chamada: o gemini-2.5-flash gera tokens de
  raciocinio internos por padrao, cobrados como output. Desligar reduz
  drasticamente o custo em bots conversacionais simples.
- Sem `max_output_tokens` no chat principal: as respostas carregam tags
  ([CONVITE], [FINALIZADO=], [TRANSFERIR=]) e podem ter varias partes; um teto
  curto truncaria essas tags. Nas chamadas auxiliares (resumo/handoff) o teto
  e seguro porque `thinking_budget=0` impede o pensamento de consumir o orcamento.
- `temperature`: 0.4 no chat, 0.2 em transcricao/analise de imagem, 0.3 nos resumos.

As chamadas do SDK sao sincronas; rodam em `asyncio.to_thread` para nao travar o
event loop.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types as gtypes

from app.client_data import load_client_data
from app.config import settings
from app.prompt import get_system_prompt, resolve_niche
from app.services.redis_service import (
    append_chat_history,
    get_chat_history,
    get_lead,
    update_lead,
)

logger = logging.getLogger(__name__)

_MODEL = "gemini-3.1-flash-lite"
_client: Optional[genai.Client] = None

# thinking_budget=0 em todas as chamadas (regra global de custo).
_THINKING_OFF = gtypes.ThinkingConfig(thinking_budget=0, include_thoughts=False)

_SP_TZ = ZoneInfo("America/Sao_Paulo")
_WEEK = [
    "segunda-feira",
    "terca-feira",
    "quarta-feira",
    "quinta-feira",
    "sexta-feira",
    "sabado",
    "domingo",
]


def _temporal_prefix() -> str:
    """Bloco de contexto temporal injetado na user_message a cada turno."""
    now = datetime.now(_SP_TZ)
    tomorrow = now + timedelta(days=1)
    return (
        f"[CONTEXTO DO SISTEMA - nao responda sobre isto, apenas use como referencia: "
        f"agora sao {now.strftime('%H:%M')} de {_WEEK[now.weekday()]}, {now.strftime('%d/%m/%Y')}. "
        f"Amanha e {_WEEK[tomorrow.weekday()]}, {tomorrow.strftime('%d/%m/%Y')}.]\n\n"
    )


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _client


def _history_to_contents(history: list[dict]) -> list[gtypes.Content]:
    contents: list[gtypes.Content] = []
    for h in history:
        role = h.get("role")
        text = (h.get("parts") or [{}])[0].get("text", "")
        if not text:
            continue
        contents.append(gtypes.Content(role=role, parts=[gtypes.Part.from_text(text=text)]))
    return contents


def _usage_tokens(response: Any) -> tuple[int, int, int]:
    meta = getattr(response, "usage_metadata", None)
    if not meta:
        return (0, 0, 0)
    inp = getattr(meta, "prompt_token_count", 0) or 0
    out = getattr(meta, "candidates_token_count", 0) or 0
    total = getattr(meta, "total_token_count", 0) or (inp + out)
    return (inp, out, total)


async def chat(phone: str, user_message: str, lead_name: str = "") -> tuple[str, tuple[int, int, int]]:
    client = _get_client()

    history = await get_chat_history(phone)
    lead = await get_lead(phone) or {}
    locked_niche = lead.get("nicho") or None

    # Lock permanente: lead inscrito pelo formulário do evento
    # (`/api/subscribe` ou auto-lock do welcome `fromMe` em consumer.py)
    # carrega `event_id` e `source=formulario_evento`. Esses leads ficam
    # travados em `lancamento_livro` para sempre. A única forma de soltar
    # é `/reset` (apaga o lead).
    if lead.get("event_id") or lead.get("source") == "formulario_evento":
        locked_niche = "lancamento_livro"
        if lead.get("nicho") != "lancamento_livro":
            await update_lead(phone, nicho="lancamento_livro")

    niche = resolve_niche(locked_niche=locked_niche)

    # Semeia o historico com as 2 mensagens iniciais que JA foram enviadas
    # (pela LP/n8n no momento da inscricao). Sem isto, no primeiro turno o
    # historico fica vazio e o modelo reenvia/reescreve a saudacao inicial.
    if not history and niche == "lancamento_livro":
        event = (load_client_data(niche=niche).get("event") or {})
        seeded = False
        for opening in (event.get("first_message"), event.get("second_message")):
            if opening:
                await append_chat_history(phone, "model", opening)
                seeded = True
        if seeded:
            history = await get_chat_history(phone)

    contents = _history_to_contents(history)
    contents.append(
        gtypes.Content(role="user", parts=[gtypes.Part.from_text(text=_temporal_prefix() + user_message)])
    )

    config = gtypes.GenerateContentConfig(
        system_instruction=get_system_prompt(niche=niche),
        temperature=0.4,
        thinking_config=_THINKING_OFF,
    )

    response = await asyncio.to_thread(
        client.models.generate_content,
        model=_MODEL,
        contents=contents,
        config=config,
    )

    ai_text = (response.text or "").strip()
    tokens = _usage_tokens(response)

    await append_chat_history(phone, "user", user_message)
    if ai_text:
        await append_chat_history(phone, "model", ai_text)

    return ai_text, tokens


async def transcribe_audio(audio_bytes: bytes) -> str:
    client = _get_client()
    response = await asyncio.to_thread(
        client.models.generate_content,
        model=_MODEL,
        contents=[
            gtypes.Content(
                role="user",
                parts=[
                    gtypes.Part.from_text(
                        text="Transcreva essa gravacao de audio fielmente. Retorne APENAS o texto transcrito, sem comentarios."
                    ),
                    gtypes.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg"),
                ],
            )
        ],
        config=gtypes.GenerateContentConfig(
            temperature=0.2,
            thinking_config=_THINKING_OFF,
        ),
    )
    return (response.text or "").strip()


async def analyze_image(image_bytes: bytes) -> str:
    client = _get_client()
    response = await asyncio.to_thread(
        client.models.generate_content,
        model=_MODEL,
        contents=[
            gtypes.Content(
                role="user",
                parts=[
                    gtypes.Part.from_text(text="Descreva esta imagem em ate 50 palavras, em portugues."),
                    gtypes.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                ],
            )
        ],
        config=gtypes.GenerateContentConfig(
            temperature=0.2,
            max_output_tokens=200,
            thinking_config=_THINKING_OFF,
        ),
    )
    return (response.text or "").strip()


async def generate_summary(phone: str) -> str:
    """Gera um resumo curto da conversa com base no historico recente."""
    history = await get_chat_history(phone)
    if not history:
        return ""

    lines = []
    for entry in history[-10:]:
        role = "Atendente" if entry.get("role") == "model" else "Lead"
        text = entry.get("parts", [{}])[0].get("text", "")
        if text:
            lines.append(f"{role}: {text[:200]}")
    if not lines:
        return ""

    client_data = load_client_data()
    business_type = (client_data.get("business", {}) or {}).get("type", "negocio")
    prompt = (
        f"Com base nesse trecho de conversa de {business_type}, "
        "escreva um resumo de 1 a 2 frases em portugues sobre quem e esse lead "
        "e qual o interesse dele. Seja objetivo.\n\n"
        + "\n".join(lines)
    )

    client = _get_client()
    try:
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=_MODEL,
            contents=[gtypes.Content(role="user", parts=[gtypes.Part.from_text(text=prompt)])],
            config=gtypes.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=150,
                thinking_config=_THINKING_OFF,
            ),
        )
        return (response.text or "").strip()
    except Exception:
        logger.exception("Erro ao gerar resumo para %s", phone)
        return ""


async def generate_handoff_summary(phone: str) -> str:
    """Gera um resumo organizado das respostas do lead para o alerta de handoff."""
    history = await get_chat_history(phone)
    if not history:
        return ""

    lines = []
    for entry in history[-30:]:
        role = "Atendente" if entry.get("role") == "model" else "Lead"
        text = entry.get("parts", [{}])[0].get("text", "")
        if text:
            lines.append(f"{role}: {text[:300]}")
    if not lines:
        return ""

    prompt = (
        "Abaixo esta a conversa entre um atendente e um lead. "
        "Monte um RESUMO OBJETIVO organizando as respostas que o LEAD deu, em formato de lista curta "
        "(uma linha por topico, no formato 'Topico: resposta'). Use apenas o que o lead respondeu; "
        "se algum topico nao foi respondido, omita. Nao invente. Nao inclua falas do atendente. "
        "Identifique os topicos a partir do que foi efetivamente discutido (ex.: nome, localidade, "
        "intencao de participar presencialmente, duvidas sobre o evento, interesse no livro, etc.).\n\n"
        "Conversa:\n" + "\n".join(lines)
    )

    client = _get_client()
    try:
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=_MODEL,
            contents=[gtypes.Content(role="user", parts=[gtypes.Part.from_text(text=prompt)])],
            config=gtypes.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=400,
                thinking_config=_THINKING_OFF,
            ),
        )
        return (response.text or "").strip()
    except Exception:
        logger.exception("Erro ao gerar resumo de handoff para %s", phone)
        return ""
