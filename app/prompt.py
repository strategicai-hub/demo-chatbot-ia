"""
Gera o SYSTEM_PROMPT a partir do template Jinja2 + dados do client.yaml.

O nicho do negócio (capital_de_giro, consorcio) é determinado dinamicamente
a partir da mensagem inicial do lead. Cada nicho tem um prompt próprio em
`app/prompts/{niche}.j2`. Se a mensagem não permitir identificar o nicho,
recai sobre `client.yaml > niche` (ou `DEFAULT_NICHE`).

`assistant.greeting` é injetado dinamicamente em cada render com base
no horário atual de São Paulo ("bom dia" / "boa tarde" / "boa noite"),
ignorando o valor presente no client.yaml.
"""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader

from app.client_data import load_client_data

DEFAULT_NICHE = "capital_de_giro"
_SP_TZ = ZoneInfo("America/Sao_Paulo")
_WEEKDAYS_PT = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


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
    """Identifica o nicho a partir do conteúdo da mensagem inicial do lead.

    Retorna o nome do nicho ("capital_de_giro" ou "consorcio") ou None se
    não for possível identificar.
    """
    if not text:
        return None
    t = text.lower()
    if "capital de giro" in t:
        return "capital_de_giro"
    if "consorcio" in t or "consórcio" in t:
        return "consorcio"
    return None


def build_prompt(niche: str | None = None) -> str:
    prompts_dir = Path(__file__).parent / "prompts"
    env = Environment(
        loader=FileSystemLoader(str(prompts_dir)),
        keep_trailing_newline=True,
    )
    data = dict(load_client_data())
    assistant = dict(data.get("assistant") or {})
    assistant["greeting"] = _compute_time_greeting()
    assistant["today_weekday"] = _compute_today_weekday()
    data["assistant"] = assistant

    resolved_niche = (niche or data.get("niche") or DEFAULT_NICHE).strip()
    template_file = f"{resolved_niche}.j2"
    if not (prompts_dir / template_file).exists():
        raise FileNotFoundError(
            f"Prompt do nicho '{resolved_niche}' não encontrado em {prompts_dir / template_file}. "
            f"Nichos disponíveis: {[p.stem for p in prompts_dir.glob('*.j2')]}"
        )
    template = env.get_template(template_file)
    return template.render(**data)


def get_system_prompt(niche: str | None = None) -> str:
    """Renderiza o prompt sob demanda (greeting reflete o horário atual)."""
    return build_prompt(niche=niche)
