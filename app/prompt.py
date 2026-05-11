"""
Gera o SYSTEM_PROMPT a partir do template Jinja2 + dados de client*.yaml.

Cadeia de resolucao do nicho ativo (1a fonte que retornar valor ganha):
  1. Override no Redis (`{PROJECT_SLUG}:active_niche`) — setado via comando
     /nicho: <nome> enviado por um numero em ADMIN_PHONES.
  2. Env var `ACTIVE_NICHE` configurada na stack (Portainer).
  3. Deteccao por palavra-chave na 1a mensagem do lead.
  4. Campo `niche` do client.yaml (ou DEFAULT_NICHE como ultima rede).

Para cada nicho, o prompt fica em `app/prompts/{niche}.j2` e os dados do negocio
podem ficar em `client.{niche}.yaml` (com fallback para `client.yaml`).

`assistant.greeting` e injetado dinamicamente em cada render com base no horario
atual de Sao Paulo ("bom dia" / "boa tarde" / "boa noite"), ignorando o valor
presente no client.yaml.
"""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import redis as redis_sync
from jinja2 import Environment, FileSystemLoader

from app.client_data import load_client_data
from app.config import settings

DEFAULT_NICHE = "capital_de_giro"
ALLOWED_NICHES = {"petshop", "capital_de_giro", "consorcio"}
NICHE_KEY = f"{settings.PROJECT_SLUG}:active_niche"

_SP_TZ = ZoneInfo("America/Sao_Paulo")
_WEEKDAYS_PT = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]

_redis_client: redis_sync.Redis | None = None


def _get_redis() -> redis_sync.Redis | None:
    """Cliente Redis sincrono lazy. Retorna None se nao for possivel conectar."""
    global _redis_client
    if _redis_client is None:
        try:
            _redis_client = redis_sync.Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
        except Exception:
            _redis_client = None
    return _redis_client


def _compute_time_greeting() -> str:
    hour = datetime.now(_SP_TZ).hour
    if 5 <= hour < 12:
        return "bom dia"
    if 12 <= hour < 18:
        return "boa tarde"
    return "boa noite"


def _compute_today_weekday() -> str:
    return _WEEKDAYS_PT[datetime.now(_SP_TZ).weekday()]


def detect_niche_from_message(text: str) -> str | None:
    """Identifica o nicho a partir do conteudo da mensagem inicial do lead.

    Retorna o nome do nicho ou None se nao for possivel identificar.
    """
    if not text:
        return None
    t = text.lower()
    if any(k in t for k in ("banho", "tosa", "pet shop", "petshop", "gato", "gata", "cachorro", "cadela")):
        return "petshop"
    if "capital de giro" in t:
        return "capital_de_giro"
    if "consorcio" in t or "consórcio" in t:
        return "consorcio"
    return None


def get_active_niche_override() -> str | None:
    """Retorna o nicho ativo gravado no Redis, se houver."""
    client = _get_redis()
    if client is None:
        return None
    try:
        value = client.get(NICHE_KEY)
        if value and value in ALLOWED_NICHES:
            return value
    except Exception:
        return None
    return None


def resolve_niche(message_text: str | None = None) -> str:
    """Aplica a cadeia de overrides e retorna o nicho a ser usado agora.

    Ordem: Redis override -> env var ACTIVE_NICHE -> deteccao por mensagem ->
    client.yaml -> DEFAULT_NICHE.
    """
    override = get_active_niche_override()
    if override:
        return override
    if settings.ACTIVE_NICHE and settings.ACTIVE_NICHE in ALLOWED_NICHES:
        return settings.ACTIVE_NICHE
    detected = detect_niche_from_message(message_text or "")
    if detected:
        return detected
    base = load_client_data()
    return base.get("niche") or DEFAULT_NICHE


def build_prompt(niche: str | None = None) -> str:
    prompts_dir = Path(__file__).parent / "prompts"
    env = Environment(
        loader=FileSystemLoader(str(prompts_dir)),
        keep_trailing_newline=True,
    )
    resolved_niche = (niche or resolve_niche()).strip()
    template_file = f"{resolved_niche}.j2"
    if not (prompts_dir / template_file).exists():
        raise FileNotFoundError(
            f"Prompt do nicho '{resolved_niche}' não encontrado em {prompts_dir / template_file}. "
            f"Nichos disponíveis: {[p.stem for p in prompts_dir.glob('*.j2')]}"
        )

    data = dict(load_client_data(niche=resolved_niche))
    assistant = dict(data.get("assistant") or {})
    assistant["greeting"] = _compute_time_greeting()
    assistant["today_weekday"] = _compute_today_weekday()
    data["assistant"] = assistant

    template = env.get_template(template_file)
    return template.render(**data)


def get_system_prompt(niche: str | None = None) -> str:
    """Renderiza o prompt sob demanda (greeting reflete o horário atual)."""
    return build_prompt(niche=niche)
