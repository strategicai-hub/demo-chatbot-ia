"""
Gera o SYSTEM_PROMPT a partir do template Jinja2 + dados de client*.yaml.

Cadeia de resolucao do nicho ativo:
  1. Nicho travado no lead/formulario, quando informado pelo codigo chamador.
  2. Override no Redis (`{PROJECT_SLUG}:active_niche`) - setado via comando
     /nicho: <nome> enviado por um numero em ADMIN_PHONES.
  3. Env var `ACTIVE_NICHE` configurada na stack (Portainer).
  4. Deteccao por palavra-chave na primeira mensagem do lead.
  5. Campo `niche` do client.yaml (ou DEFAULT_NICHE como ultima rede).

Para cada nicho, o prompt fica em `app/prompts/{niche}.j2` e os dados do
negocio podem ficar em `client.{niche}.yaml` (com fallback para `client.yaml`).

`assistant.greeting` e injetado dinamicamente em cada render com base no horario
atual de Sao Paulo ("bom dia" / "boa tarde" / "boa noite"), ignorando o valor
presente no client.yaml.
"""
from datetime import datetime
from pathlib import Path
import unicodedata
from zoneinfo import ZoneInfo

import redis as redis_sync
from jinja2 import Environment, FileSystemLoader

from app.client_data import load_client_data
from app.config import settings
from app.services import sai_sync

DEFAULT_NICHE = "capital_de_giro"
ALLOWED_NICHES = {
    "petshop",
    "capital_de_giro",
    "consorcio",
    "material_construcao",
    "lancamento_livro",
}
NICHE_KEY = f"{settings.PROJECT_SLUG}:active_niche"

_SP_TZ = ZoneInfo("America/Sao_Paulo")
_WEEKDAYS_PT = ["segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo"]
_WEEKDAYS_PT_FULL = [
    "Domingo",
    "Segunda-feira",
    "Terca-feira",
    "Quarta-feira",
    "Quinta-feira",
    "Sexta-feira",
    "Sabado",
]  # indice = weekday do snapshot (0=Domingo .. 6=Sabado, igual ao painel SAI)

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


def _fold_text(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def _compute_time_greeting() -> str:
    hour = datetime.now(_SP_TZ).hour
    if 5 <= hour < 12:
        return "bom dia"
    if 12 <= hour < 18:
        return "boa tarde"
    return "boa noite"


def _compute_today_weekday() -> str:
    return _WEEKDAYS_PT[datetime.now(_SP_TZ).weekday()]


def _compute_time_context_block() -> str:
    """Bloco autoritativo de data/hora atual em Sao Paulo.

    Injetado no final do prompt para reduzir erros do modelo em datas relativas.
    Inclui hoje + ontem + amanha ja computados.
    """
    from datetime import timedelta

    week = [
        "segunda-feira",
        "terca-feira",
        "quarta-feira",
        "quinta-feira",
        "sexta-feira",
        "sabado",
        "domingo",
    ]
    now = datetime.now(_SP_TZ)
    yesterday = now - timedelta(days=1)
    tomorrow = now + timedelta(days=1)
    return (
        "\n\n---\n\n## DATA E HORA ATUAIS - REGRA ABSOLUTA\n"
        "Estas informacoes sao AUTORITATIVAS. Substituem qualquer suposicao sua. "
        "Use-as sempre que for falar de dia, data, hoje, ontem, amanha, semana ou horario:\n\n"
        f"- AGORA (America/Sao_Paulo): {now.strftime('%d/%m/%Y %H:%M')}\n"
        f"- HOJE e {week[now.weekday()]} ({now.strftime('%d/%m/%Y')}).\n"
        f"- ONTEM foi {week[yesterday.weekday()]} ({yesterday.strftime('%d/%m/%Y')}).\n"
        f"- AMANHA sera {week[tomorrow.weekday()]} ({tomorrow.strftime('%d/%m/%Y')}).\n\n"
        "PROIBIDO inventar outro dia da semana. Se for mencionar \"amanha\", "
        f"obrigatoriamente e {week[tomorrow.weekday()]}.\n"
    )


def detect_niche_from_message(text: str) -> str | None:
    """Identifica o nicho a partir do conteudo da mensagem inicial do lead."""
    if not text:
        return None
    t = _fold_text(text)

    if any(
        k in t
        for k in (
            "comunicacao humanizada",
            "neurocomunicacao",
            "lancamento do livro",
            "livro comunicacao",
            "livro comunicacao humanizada",
            "evento comunicacao",
            "ikigai",
            "eduardo almeida",
        )
    ):
        return "lancamento_livro"

    if any(k in t for k in ("banho", "tosa", "pet shop", "petshop", "gato", "gata", "cachorro", "cadela")):
        return "petshop"
    if "capital de giro" in t:
        return "capital_de_giro"
    if "consorcio" in t:
        return "consorcio"
    if any(
        k in t
        for k in (
            "cimento",
            "areia",
            "brita",
            "tijolo",
            "bloco",
            "argamassa",
            "material de construcao",
            "construcao",
            "tinta",
            "massa corrida",
            "cano pvc",
            "tubo pvc",
            "disjuntor",
            "fio eletrico",
            "reforma",
            "obra",
        )
    ):
        return "material_construcao"
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


def resolve_niche(message_text: str | None = None, locked_niche: str | None = None) -> str:
    """Aplica a cadeia de overrides e retorna o nicho a ser usado agora."""
    if locked_niche and locked_niche in ALLOWED_NICHES:
        return locked_niche

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


def _format_business_hours(hours: list[dict]) -> str:
    """Converte o snapshot do painel em texto legivel para o prompt."""
    lines: list[str] = []
    for day in hours or []:
        if not isinstance(day, dict):
            continue
        wd = day.get("weekday")
        windows = day.get("windows") or []
        if not isinstance(wd, int) or not 0 <= wd < 7 or not windows:
            continue
        ranges = [
            f"{w['start']}-{w['end']}"
            for w in windows
            if isinstance(w, dict) and w.get("start") and w.get("end")
        ]
        if ranges:
            lines.append(f"- {_WEEKDAYS_PT_FULL[wd]}: {', '.join(ranges)}")
    if not lines:
        return ""
    return "Horario de funcionamento:\n" + "\n".join(lines)


def _format_products(products: list[dict]) -> str:
    items: list[str] = []
    for p in products or []:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        name = str(p["name"]).strip()
        price = p.get("priceCents")
        if isinstance(price, int) and price > 0:
            price_str = f"R$ {price / 100:.2f}".replace(".", ",")
        else:
            price_str = "consultar"
        desc = (p.get("description") or "").strip()
        line = f"- {name} - {price_str}"
        if desc:
            line += f" ({desc})"
        items.append(line)
    if not items:
        return ""
    return "Produtos e servicos:\n" + "\n".join(items)


def _apply_sai_snapshot(data: dict, assistant: dict) -> tuple[dict, str]:
    """Funde snapshot do Painel IA (Redis) e devolve (assistant, suffix do prompt)."""
    snap = sai_sync.load_snapshot_sync()
    if not snap or not isinstance(snap, dict):
        return assistant, ""
    snap_assistant = snap.get("assistant") or {}
    display = snap_assistant.get("displayName")
    if display:
        assistant["name"] = display
    suffix_blocks: list[str] = []
    hours_block = _format_business_hours(snap_assistant.get("businessHours") or [])
    if hours_block:
        suffix_blocks.append(hours_block)
    products_block = _format_products(snap.get("products") or [])
    if products_block:
        suffix_blocks.append(products_block)
    suffix = ("\n\n" + "\n\n".join(suffix_blocks)) if suffix_blocks else ""
    return assistant, suffix


def build_prompt(niche: str | None = None) -> str:
    prompts_dir = Path(__file__).parent / "prompts"
    env = Environment(
        loader=FileSystemLoader(str(prompts_dir)),
        keep_trailing_newline=True,
    )
    resolved_niche = (niche or "").strip() or resolve_niche()
    template_file = f"{resolved_niche}.j2"
    if not (prompts_dir / template_file).exists():
        raise FileNotFoundError(
            f"Prompt do nicho '{resolved_niche}' nao encontrado em {prompts_dir / template_file}. "
            f"Nichos disponiveis: {[p.stem for p in prompts_dir.glob('*.j2')]}"
        )

    data = dict(load_client_data(niche=resolved_niche))
    assistant = dict(data.get("assistant") or {})
    assistant["greeting"] = _compute_time_greeting()
    assistant["today_weekday"] = _compute_today_weekday()

    assistant, sai_suffix = _apply_sai_snapshot(data, assistant)
    data["assistant"] = assistant

    template = env.get_template(template_file)
    rendered = template.render(**data)
    return rendered + sai_suffix + _compute_time_context_block()


def get_system_prompt(niche: str | None = None) -> str:
    """Renderiza o prompt sob demanda (greeting reflete o horario atual)."""
    return build_prompt(niche=niche)
