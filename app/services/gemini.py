import logging

import google.generativeai as genai

from app.client_data import load_client_data
from app.config import settings
from app.prompt import get_system_prompt, resolve_niche
from app.services.redis_service import get_chat_history, append_chat_history

logger = logging.getLogger(__name__)

_configured = False


def _ensure_configured() -> None:
    global _configured
    if not _configured:
        genai.configure(api_key=settings.GEMINI_API_KEY)
        _configured = True


async def chat(phone: str, user_message: str, lead_name: str = "") -> tuple[str, tuple[int, int, int]]:
    _ensure_configured()

    history = await get_chat_history(phone)

    first_user_text = next(
        (
            entry.get("parts", [{}])[0].get("text", "")
            for entry in history
            if entry.get("role") == "user"
        ),
        user_message,
    )
    niche = resolve_niche(first_user_text)

    model = genai.GenerativeModel(
        model_name="gemini-2.5-flash",
        system_instruction=get_system_prompt(niche=niche),
    )

    chat_session = model.start_chat(history=history)
    response = chat_session.send_message(user_message)
    ai_text = response.text.strip() if response.text else ""

    tokens = (0, 0, 0)
    if hasattr(response, "usage_metadata") and response.usage_metadata:
        meta = response.usage_metadata
        inp = meta.prompt_token_count or 0
        out = meta.candidates_token_count or 0
        tokens = (inp, out, meta.total_token_count or inp + out)

    await append_chat_history(phone, "user", user_message)
    if ai_text:
        await append_chat_history(phone, "model", ai_text)

    return ai_text, tokens


async def transcribe_audio(audio_bytes: bytes) -> str:
    _ensure_configured()
    model = genai.GenerativeModel("gemini-2.5-flash")

    audio_part = {
        "mime_type": "audio/ogg",
        "data": audio_bytes,
    }
    response = model.generate_content(
        ["Transcreva essa gravacao de audio fielmente. Retorne APENAS o texto transcrito, sem comentarios.", audio_part]
    )
    return response.text.strip() if response.text else ""


async def generate_summary(phone: str) -> str:
    """Gera um resumo curto da conversa com base no historico recente."""
    _ensure_configured()
    history = await get_chat_history(phone)
    if not history:
        return ""

    # Monta texto das ultimas 10 mensagens para resumir
    lines = []
    for entry in history[-10:]:
        role = "Atendente" if entry.get("role") == "model" else "Lead"
        text = entry.get("parts", [{}])[0].get("text", "")
        if text:
            lines.append(f"{role}: {text[:200]}")

    if not lines:
        return ""

    model = genai.GenerativeModel("gemini-2.5-flash")
    client = load_client_data()
    business_type = (client.get("business", {}) or {}).get("type", "negocio")
    prompt = (
        f"Com base nesse trecho de conversa de {business_type}, "
        "escreva um resumo de 1 a 2 frases em portugues sobre quem e esse lead "
        "e qual o interesse dele. Seja objetivo.\n\n"
        + "\n".join(lines)
    )
    try:
        response = model.generate_content(prompt)
        return response.text.strip() if response.text else ""
    except Exception:
        return ""


async def generate_handoff_summary(phone: str) -> str:
    """Gera um resumo organizado das respostas do lead para o alerta de handoff.

    Diferente de `generate_summary` (1-2 frases), aqui listamos ponto a ponto
    o que o lead informou, para o consultor humano bater o olho e agir.
    """
    _ensure_configured()
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

    model = genai.GenerativeModel("gemini-2.5-flash")
    prompt = (
        "Abaixo esta a conversa entre um atendente e um lead. "
        "Monte um RESUMO OBJETIVO organizando as respostas que o LEAD deu, em formato de lista curta "
        "(uma linha por topico, no formato 'Topico: resposta'). Use apenas o que o lead respondeu; "
        "se algum topico nao foi respondido, omita. Nao invente. Nao inclua falas do atendente. "
        "Identifique os topicos a partir do que foi efetivamente discutido (ex.: nome, localidade, "
        "tempo fora do Brasil, intencao de retorno, tipo de investimento, renda mensal, valor de "
        "investimento mensal, documentos no Brasil, conta bancaria, endereco de referencia, telefone "
        "de referencia, valor necessario, bem a alienar, finalidade do credito, parcela maxima, "
        "urgencia, etc.).\n\n"
        "Conversa:\n" + "\n".join(lines)
    )
    try:
        response = model.generate_content(prompt)
        return response.text.strip() if response.text else ""
    except Exception:
        return ""


async def analyze_image(image_bytes: bytes) -> str:
    _ensure_configured()
    model = genai.GenerativeModel("gemini-2.5-flash")

    image_part = {
        "mime_type": "image/jpeg",
        "data": image_bytes,
    }
    response = model.generate_content(
        ["Descreva esta imagem em ate 50 palavras, em portugues.", image_part]
    )
    return response.text.strip() if response.text else ""
