from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import prompt as prompt_mod
from app.api import EventSubscription, _handle_event_subscription
from app.consumer import _book_price_response, _is_book_price_question, _is_refund_policy_question
from app.prompt import build_prompt, detect_niche_from_message, resolve_niche
from app.services import event_reminders


def test_detects_lancamento_livro_from_form_message():
    text = "Lead vindo do formulário do evento Comunicação Humanizada da Ikigai"
    assert detect_niche_from_message(text) == "lancamento_livro"


def test_locked_lancamento_livro_wins_over_later_pet_terms():
    assert resolve_niche("Quero saber de banho e tosa", locked_niche="lancamento_livro") == "lancamento_livro"


def test_builds_lancamento_livro_prompt(monkeypatch):
    monkeypatch.setattr(prompt_mod.sai_sync, "load_snapshot_sync", lambda: None)
    rendered = build_prompt("lancamento_livro")
    assert "assistente virtual do Lançamento do livro Comunicação Humanizada" in rendered
    assert "mensagens curtas" in rendered.lower()
    assert "dedicatória exclusiva" in rendered
    assert "Tópicos abordados no livro" in rendered
    assert "não encerre uma resposta sem fazer uma pergunta clara" in rendered
    assert (
        "A propósito, para quem se inscreveu agora, temos uma oportunidade especial: "
        "o livro físico com dedicatória exclusiva do Eduardo Almeida. "
        "Gostaria de garantir seu exemplar autografado?"
    ) in rendered
    assert "Que ótimo! Para a dedicatória, qual seria o nome completo que você gostaria no livro?" in rendered
    assert "Qualquer dúvida, é só me chamar por aqui" in rendered
    assert "NUNCA prometa enviar lembretes" in rendered
    assert "Quando estivermos mais perto da data" not in rendered
    assert "O pedido de reembolso pode ser feito em até 7 dias" in rendered
    assert "Valor do livro físico com dedicatória: R$ 49,50" in rendered
    assert "O livro físico com dedicatória exclusiva está R$ 49,50." in rendered
    assert "sempre responda à pergunta explícita do lead" in rendered
    assert "Não ignore a pergunta para voltar ao roteiro." in rendered
    assert "https://www.asaas.com/c/sdxswrpyl5gjvt8z" in rendered


def test_detects_refund_policy_questions():
    assert _is_refund_policy_question("Qual é a política de reembolso do livro?")
    assert _is_refund_policy_question("Posso pedir estorno se eu desistir?")
    assert not _is_refund_policy_question("Quero comprar o livro com dedicatória")


def test_detects_book_price_questions_after_offer():
    history = [
        {
            "role": "model",
            "parts": [
                {
                    "text": (
                        "A propósito, para quem se inscreveu agora, temos uma oportunidade especial: "
                        "o livro físico com dedicatória exclusiva do Eduardo Almeida."
                    )
                }
            ],
        }
    ]

    assert _is_book_price_question("Legal. Quanto é?", history)
    assert _is_book_price_question("Qual o valor do livro?")
    assert not _is_book_price_question("Quanto é a inscrição?", history)
    assert "R$ 49,50" in _book_price_response()


def test_presence_confirmation_parser():
    assert event_reminders.parse_presence_confirmation("Sim, pode confirmar minha presença") == "sim"
    assert event_reminders.parse_presence_confirmation("Infelizmente não vou conseguir") == "não"
    assert event_reminders.parse_presence_confirmation("Talvez, vou ver") is None


def test_reminder_schedule_is_inside_configured_window():
    cfg = {"date": "2026-06-04", "start": "12:00", "end": "18:00"}
    scheduled = event_reminders.scheduled_at("5511999990000", "day_before", cfg)
    tz = ZoneInfo("America/Sao_Paulo")
    assert datetime(2026, 6, 4, 12, 0, tzinfo=tz) <= scheduled <= datetime(2026, 6, 4, 18, 0, tzinfo=tz)


def test_post_event_survey_schedule_is_inside_configured_window():
    cfg = {"date": "2026-06-06", "start": "09:00", "end": "11:30"}
    scheduled = event_reminders.scheduled_at("5511999990000", "post_event_survey", cfg)
    tz = ZoneInfo("America/Sao_Paulo")
    assert datetime(2026, 6, 6, 9, 0, tzinfo=tz) <= scheduled <= datetime(2026, 6, 6, 11, 30, tzinfo=tz)


def test_post_event_survey_parser_extracts_score_and_learning():
    score, learning = event_reminders.parse_post_event_survey(
        "1) 9\n2) Meu maior aprendizado foi ouvir com mais empatia."
    )
    assert score == "9"
    assert learning == "Meu maior aprendizado foi ouvir com mais empatia."


@pytest.mark.asyncio
async def test_updates_post_event_survey_after_survey_reminder(monkeypatch):
    updates = []

    async def fake_update_lead(phone, **fields):
        updates.append((phone, fields))

    monkeypatch.setattr("app.services.event_reminders.update_lead", fake_update_lead)

    lead = {
        "nicho": "lancamento_livro",
        "last_reminder": "post_event_survey",
    }
    result = await event_reminders.update_post_event_survey_from_message(
        "5511999990000",
        lead,
        "10. Aprendi que conversar melhor exige escuta real.",
    )

    assert result == ("10", "Aprendi que conversar melhor exige escuta real")
    assert updates[0][1]["survey_score"] == "10"
    assert updates[0][1]["survey_learning"] == "Aprendi que conversar melhor exige escuta real"


@pytest.mark.asyncio
async def test_form_subscription_locks_event_niche_and_sends_initial_messages(monkeypatch):
    updates = []
    sent = []
    history = []

    async def fake_get_lead(phone):
        return {}

    async def fake_update_lead(phone, **fields):
        updates.append((phone, fields))

    async def fake_clear_history(phone):
        raise AssertionError("Nao deveria limpar historico sem nicho anterior")

    async def fake_delete_buffer(phone):
        raise AssertionError("Nao deveria limpar buffer sem nicho anterior")

    async def fake_send_text(phone, text, delay=4000):
        sent.append((phone, text, delay))
        return {"ok": True}

    async def fake_append_history(phone, role, text):
        history.append((phone, role, text))

    monkeypatch.setattr("app.api.rds.get_lead", fake_get_lead)
    monkeypatch.setattr("app.api.rds.update_lead", fake_update_lead)
    monkeypatch.setattr("app.api.rds.clear_chat_history", fake_clear_history)
    monkeypatch.setattr("app.api.rds.delete_buffer", fake_delete_buffer)
    monkeypatch.setattr("app.api.uazapi.send_text", fake_send_text)
    monkeypatch.setattr("app.api.rds.append_chat_history", fake_append_history)

    payload = EventSubscription(
        name="Ana Silva",
        email="ana@example.com",
        phone="(11) 99999-0000",
        profession="RH",
    )
    response = await _handle_event_subscription(payload, None)

    assert response["ok"] is True
    assert updates[0][0] == "5511999990000"
    assert updates[0][1]["nicho"] == "lancamento_livro"
    assert updates[0][1]["confirmado"] == ""
    assert len(sent) == 2
    assert sent[0][1].startswith("Olá! Sou a Mya")
    assert sent[1][1] == "Queremos te conhecer melhor. O que te motivou a se inscrever no nosso evento?"
    assert [item[1] for item in history] == ["model", "model"]
