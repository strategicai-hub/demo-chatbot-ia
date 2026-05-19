"""
Rotas de observabilidade/logs para painel externo.
Prefixo derivado de settings.WEBHOOK_PATH
"""
from datetime import datetime
import hmac
import json
import logging
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.config import settings
from app.services import event_reminders
from app.services import redis_keys as keys
from app.services import redis_service as rds
from app.services import uazapi

logger = logging.getLogger(__name__)
router = APIRouter(prefix=settings.WEBHOOK_PATH)
public_router = APIRouter()
_SP_TZ = ZoneInfo("America/Sao_Paulo")


class EventSubscription(BaseModel):
    name: str
    email: str
    phone: str
    profession: str = ""
    referred_by: str = ""
    session_id: str = ""
    path: str = ""
    utm_source: str = ""
    utm_medium: str = ""
    utm_campaign: str = ""
    gclid: str = ""


def _verify_form_secret(x_form_secret: str | None) -> None:
    expected = settings.FORM_WEBHOOK_SECRET
    if expected and (not x_form_secret or not hmac.compare_digest(x_form_secret, expected)):
        raise HTTPException(status_code=401, detail="invalid form secret")


async def _handle_event_subscription(
    payload: EventSubscription,
    x_form_secret: str | None,
) -> dict:
    _verify_form_secret(x_form_secret)
    try:
        phone = event_reminders.normalize_phone(payload.phone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    name = payload.name.strip()
    email = payload.email.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Nome obrigatorio")
    if not email:
        raise HTTPException(status_code=400, detail="Email obrigatorio")

    now = datetime.now(_SP_TZ).isoformat(timespec="seconds")
    existing = await rds.get_lead(phone) or {}
    if existing.get("nicho") and existing.get("nicho") != event_reminders.EVENT_NICHE:
        await rds.clear_chat_history(phone)
        await rds.delete_buffer(phone)

    await rds.update_lead(
        phone,
        name=name,
        email=email,
        profession=payload.profession.strip(),
        referred_by=payload.referred_by.strip(),
        nicho=event_reminders.EVENT_NICHE,
        event_id=event_reminders.event_id(),
        status_conversa="Inscrito",
        source="formulario_evento",
        session_id=payload.session_id.strip(),
        landing_path=payload.path.strip(),
        utm_source=payload.utm_source.strip(),
        utm_medium=payload.utm_medium.strip(),
        utm_campaign=payload.utm_campaign.strip(),
        gclid=payload.gclid.strip(),
        inscrito_em=existing.get("inscrito_em") or now,
        atualizado_em=now,
        confirmado=existing.get("confirmado", ""),
    )

    first_message, second_message = event_reminders.registration_messages()
    try:
        if first_message:
            await uazapi.send_text(phone, first_message, delay=1000)
            await rds.append_chat_history(phone, "model", first_message)
        if second_message:
            await uazapi.send_text(phone, second_message, delay=3500)
            await rds.append_chat_history(phone, "model", second_message)
        await rds.update_lead(phone, event_welcome_sent_at=now)
    except Exception as exc:
        logger.exception("Erro ao enviar mensagens iniciais do evento para %s", phone)
        raise HTTPException(
            status_code=502,
            detail="Inscricao registrada, mas nao foi possivel enviar o WhatsApp agora.",
        ) from exc

    return {
        "ok": True,
        "message": "Inscrição confirmada! Enviamos uma mensagem no seu WhatsApp.",
        "phone": phone,
    }


@router.post("/api/subscribe")
async def subscribe_event(
    payload: EventSubscription,
    x_form_secret: str | None = Header(default=None, alias="x-form-secret"),
):
    return await _handle_event_subscription(payload, x_form_secret)


@public_router.post("/api/subscribe")
async def subscribe_event_public(
    payload: EventSubscription,
    x_form_secret: str | None = Header(default=None, alias="x-form-secret"),
):
    return await _handle_event_subscription(payload, x_form_secret)


@router.get("/logs/leads")
async def logs_leads():
    """Retorna todos os leads com dados de CRM."""
    r = await rds.get_redis()

    lead_keys = await r.keys(keys.lead_scan_pattern())
    history_keys = await r.keys(keys.history_scan_pattern())

    phones: set[str] = set()
    for k in lead_keys:
        phones.add(keys.phone_from_lead_key(k))
    for k in history_keys:
        phones.add(keys.phone_from_history_key(k))

    leads = []
    for phone in sorted(phones):
        crm = await r.hgetall(keys.lead_key(phone))
        msg_count = await r.llen(keys.history_key(phone))
        has_followup = await r.exists(keys.followup_active_key(phone)) == 1
        leads.append({
            "phone": phone,
            "nome": crm.get("name", ""),
            "nicho": crm.get("nicho", ""),
            "resumo": crm.get("resumo", ""),
            "event_id": crm.get("event_id", ""),
            "msg_count": msg_count,
            "has_followup": has_followup,
        })

    leads.sort(key=lambda x: x["msg_count"], reverse=True)
    return leads


@router.get("/logs/history/{phone}")
async def logs_history(phone: str):
    """Retorna o historico de mensagens de um lead."""
    r = await rds.get_redis()

    raw = await r.lrange(keys.history_key(phone), 0, -1)
    messages = []
    for item in raw:
        try:
            entry = json.loads(item)
            messages.append({
                "role": entry.get("type", ""),
                "content": entry.get("data", {}).get("content", ""),
            })
        except Exception:
            pass
    return messages


@router.get("/logs/events")
async def logs_events(limit: int = 100):
    """Retorna os ultimos N eventos de execucao do worker."""
    r = await rds.get_redis()

    raw = await r.lrange(keys.session_log_key(), 0, limit - 1)
    events = []
    for item in raw:
        try:
            events.append(json.loads(item))
        except Exception:
            pass
    return events


def _format_confirmado(value: str) -> str:
    if value == "sim":
        return "Sim"
    if value == "não":
        return "Não"
    return "Pendente"


@public_router.get("/admin/inscritos")
@router.get("/admin/inscritos")
async def admin_inscritos():
    """Lista de inscritos do evento Comunicação Humanizada."""
    r = await rds.get_redis()
    lead_keys = await r.keys(keys.lead_scan_pattern())
    leads = []

    for lead_key in lead_keys:
        crm = await r.hgetall(lead_key)
        if crm.get("nicho") != event_reminders.EVENT_NICHE:
            continue
        phone = keys.phone_from_lead_key(lead_key)
        leads.append(
            {
                "nome": crm.get("name", ""),
                "phone": phone,
                "email": crm.get("email", ""),
                "profession": crm.get("profession", ""),
                "referred_by": crm.get("referred_by", ""),
                "confirmado": _format_confirmado(crm.get("confirmado", "")),
                "confirmado_raw": crm.get("confirmado", ""),
                "survey_score": crm.get("survey_score", ""),
                "survey_learning": crm.get("survey_learning", ""),
                "survey_answered_at": crm.get("survey_answered_at", ""),
                "inscrito_em": crm.get("inscrito_em", ""),
                "atualizado_em": crm.get("atualizado_em", ""),
                "last_reminder": crm.get("last_reminder", ""),
            }
        )

    leads.sort(key=lambda x: x["inscrito_em"], reverse=True)
    return leads


@public_router.get("/admin", response_class=HTMLResponse)
@router.get("/admin", response_class=HTMLResponse)
async def admin():
    """Painel simples de inscritos com filtro e ordenacao."""
    html = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__BNAME__ - Inscritos</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; background: #f7f4ee; color: #211f1a; font-family: Inter, Arial, sans-serif; }
  header { background: #16130f; color: #fff; padding: 22px 28px; }
  header h1 { margin: 0 0 6px; font-size: 22px; }
  header p { margin: 0; color: #d5c7aa; font-size: 13px; }
  main { padding: 24px 28px; }
  .toolbar { display: flex; gap: 12px; align-items: center; margin-bottom: 16px; flex-wrap: wrap; }
  input { width: min(420px, 100%); border: 1px solid #cfc7b8; border-radius: 6px; padding: 10px 12px; font-size: 14px; background: #fff; }
  .count { color: #645b4c; font-size: 13px; }
  .table-wrap { overflow-x: auto; background: #fff; border: 1px solid #ddd4c4; border-radius: 8px; }
  table { width: 100%; border-collapse: collapse; min-width: 1220px; }
  th, td { padding: 11px 12px; border-bottom: 1px solid #eee7db; text-align: left; font-size: 13px; vertical-align: top; }
  th { background: #f1eadf; color: #40382d; position: sticky; top: 0; z-index: 1; }
  th button { all: unset; cursor: pointer; font-weight: 700; display: inline-flex; gap: 6px; align-items: center; }
  tr:hover td { background: #fffaf1; }
  .badge { display: inline-block; border-radius: 999px; padding: 4px 9px; font-size: 12px; font-weight: 700; }
  .sim { background: #dff3e4; color: #176b31; }
  .nao { background: #fde1dd; color: #9d2b1f; }
  .pendente { background: #f0e7d4; color: #72561f; }
  .muted { color: #7b7162; }
</style>
</head>
<body>
<header>
  <h1>Inscritos - Comunicação Humanizada</h1>
  <p>Evento gratuito - 05/06/2026 - 18:30</p>
</header>
<main>
  <div class="toolbar">
    <input id="filter" type="search" placeholder="Filtrar por nome, telefone, email, profissão ou confirmação">
    <span class="count" id="count">Carregando...</span>
  </div>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th><button data-key="nome">Nome <span></span></button></th>
          <th><button data-key="phone">WhatsApp <span></span></button></th>
          <th><button data-key="email">Email <span></span></button></th>
          <th><button data-key="profession">Profissão <span></span></button></th>
          <th><button data-key="referred_by">Indicação <span></span></button></th>
          <th><button data-key="confirmado">Confirmado <span></span></button></th>
          <th><button data-key="survey_score">Nota pós-evento <span></span></button></th>
          <th><button data-key="survey_learning">Maior aprendizado <span></span></button></th>
          <th><button data-key="inscrito_em">Inscrito em <span></span></button></th>
          <th><button data-key="last_reminder">Último lembrete <span></span></button></th>
        </tr>
      </thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
</main>
<script>
const state = { rows: [], sortKey: "inscrito_em", sortDir: "desc", filter: "" };
const rowsEl = document.getElementById("rows");
const countEl = document.getElementById("count");
const filterEl = document.getElementById("filter");

function normalize(value) {
  return String(value || "").normalize("NFD").replace(/[\\u0300-\\u036f]/g, "").toLowerCase();
}

function esc(value) {
  return String(value || "").replace(/[&<>"']/g, ch => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  })[ch]);
}

function badge(value) {
  const key = normalize(value);
  const cls = key === "sim" ? "sim" : key === "nao" ? "nao" : "pendente";
  return `<span class="badge ${cls}">${esc(value || "Pendente")}</span>`;
}

function formatDate(value) {
  if (!value) return '<span class="muted">-</span>';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return esc(value);
  return date.toLocaleString("pt-BR", { timeZone: "America/Sao_Paulo" });
}

function filteredRows() {
  const term = normalize(state.filter);
  let rows = state.rows;
  if (term) {
    rows = rows.filter(row => normalize(Object.values(row).join(" ")).includes(term));
  }
  rows = [...rows].sort((a, b) => {
    const av = normalize(a[state.sortKey]);
    const bv = normalize(b[state.sortKey]);
    if (av < bv) return state.sortDir === "asc" ? -1 : 1;
    if (av > bv) return state.sortDir === "asc" ? 1 : -1;
    return 0;
  });
  return rows;
}

function render() {
  const rows = filteredRows();
  rowsEl.innerHTML = "";
  for (const row of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${row.nome ? esc(row.nome) : '<span class="muted">-</span>'}</td>
      <td>${esc(row.phone)}</td>
      <td>${row.email ? esc(row.email) : '<span class="muted">-</span>'}</td>
      <td>${row.profession ? esc(row.profession) : '<span class="muted">-</span>'}</td>
      <td>${row.referred_by ? esc(row.referred_by) : '<span class="muted">-</span>'}</td>
      <td>${badge(row.confirmado)}</td>
      <td>${row.survey_score ? esc(row.survey_score) : '<span class="muted">-</span>'}</td>
      <td>${row.survey_learning ? esc(row.survey_learning) : '<span class="muted">-</span>'}</td>
      <td>${formatDate(row.inscrito_em)}</td>
      <td>${row.last_reminder ? esc(row.last_reminder) : '<span class="muted">-</span>'}</td>
    `;
    rowsEl.appendChild(tr);
  }
  countEl.textContent = `${rows.length} de ${state.rows.length} inscrito(s)`;
  document.querySelectorAll("th button").forEach(btn => {
    const span = btn.querySelector("span");
    span.textContent = btn.dataset.key === state.sortKey ? (state.sortDir === "asc" ? "↑" : "↓") : "";
  });
}

document.querySelectorAll("th button").forEach(btn => {
  btn.addEventListener("click", () => {
    const key = btn.dataset.key;
    if (state.sortKey === key) state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
    else { state.sortKey = key; state.sortDir = "asc"; }
    render();
  });
});

filterEl.addEventListener("input", () => {
  state.filter = filterEl.value;
  render();
});

async function load() {
  const urls = ["__WPATH__/admin/inscritos", "/admin/inscritos"];
  let response = null;
  for (const url of urls) {
    response = await fetch(url);
    if (response.ok) break;
  }
  if (!response || !response.ok) throw new Error("HTTP " + (response ? response.status : "0"));
  state.rows = await response.json();
  render();
}

load().catch(error => {
  countEl.textContent = "Erro ao carregar: " + error.message;
});
</script>
</body>
</html>"""
    return html.replace("__BNAME__", settings.BUSINESS_NAME).replace("__WPATH__", settings.WEBHOOK_PATH)


@router.get("/painel", response_class=HTMLResponse)
async def painel():
    """Painel de logs em tempo real."""
    bname = settings.BUSINESS_NAME
    wpath = settings.WEBHOOK_PATH
    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<title>{bname} — Painel</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #111; color: #e0e0e0; font-family: 'Courier New', monospace; padding: 20px; }}
  h1 {{ color: #e67e22; font-size: 18px; margin-bottom: 4px; }}
  #status {{ font-size: 11px; color: #666; margin-bottom: 16px; }}
  .event {{ border: 1px solid #2a2a2a; border-radius: 6px; padding: 10px 14px; margin-bottom: 10px; background: #1a1a1a; }}
  .event-header {{ color: #555; font-size: 11px; margin-bottom: 8px; border-bottom: 1px solid #2a2a2a; padding-bottom: 5px; }}
  .event-header .phone {{ color: #3498db; font-weight: bold; }}
  .log-line {{ margin: 3px 0; font-size: 12px; line-height: 1.5; }}
  .new-badge {{ display: inline-block; background: #27ae60; color: #fff; font-size: 10px; padding: 1px 5px; border-radius: 3px; margin-left: 8px; }}
</style>
</head>
<body>
<h1>{bname} — Execucoes</h1>
<div id="status">Carregando...</div>
<div id="events"></div>
<script>
let lastTs = null;

function fmt(ts) {{
  return new Date(ts * 1000).toLocaleString('pt-BR', {{ timeZone: 'America/Sao_Paulo' }});
}}

async function refresh() {{
  try {{
    const res = await fetch('{wpath}/logs/events?limit=50');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const events = await res.json();
    const container = document.getElementById('events');
    const status = document.getElementById('status');

    if (!events.length) {{
      status.textContent = 'Nenhuma execucao registrada ainda.';
      return;
    }}

    const newest = events[0].ts;
    const isNew = newest !== lastTs;

    if (isNew) {{
      container.innerHTML = '';
      for (let i = 0; i < events.length; i++) {{
        const ev = events[i];
        const div = document.createElement('div');
        div.className = 'event';

        const header = document.createElement('div');
        header.className = 'event-header';
        header.innerHTML = fmt(ev.ts) + ' &nbsp;—&nbsp; <span class="phone">' + (ev.phone || '') + '</span>'
          + (i === 0 && lastTs !== null ? '<span class="new-badge">NOVO</span>' : '');
        div.appendChild(header);

        for (const line of (ev.lines || [])) {{
          const p = document.createElement('p');
          p.className = 'log-line';
          p.innerHTML = line;
          div.appendChild(p);
        }}
        container.appendChild(div);
      }}
      lastTs = newest;
    }}

    const now = new Date().toLocaleTimeString('pt-BR');
    status.textContent = 'Atualizado: ' + now + ' · ' + events.length + ' execucao(oes)';
  }} catch (e) {{
    document.getElementById('status').textContent = 'Erro: ' + e.message;
  }}
}}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""
    return html
