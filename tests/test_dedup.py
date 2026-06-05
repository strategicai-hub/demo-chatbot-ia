"""
Idempotencia por mensagem: o mesmo webhook entregue 2x (UAZAPI reenvia ou o
relay duplica) deve gerar UMA resposta so. A segunda entrega e descartada cedo,
antes de bufferizar / acionar o Gemini.
"""
import pytest

from app import consumer


@pytest.mark.asyncio
async def test_duplicate_message_id_is_discarded(monkeypatch):
    seen: set[str] = set()
    processed_after_dedup = []

    async def fake_mark(fingerprint, ttl=600):
        if fingerprint in seen:
            return False
        seen.add(fingerprint)
        return True

    async def fake_get_lead(phone):
        processed_after_dedup.append(phone)
        return {"name": "X"}

    # Stub do conftest e sobrescrito aqui pelo dedup real (set em memoria).
    monkeypatch.setattr(consumer.rds, "mark_message_processed", fake_mark)
    # Se passar do dedup, get_lead seria chamado — usamos como sentinela.
    monkeypatch.setattr(consumer.rds, "get_lead", fake_get_lead)
    monkeypatch.setattr(consumer.rds, "is_blocked", lambda phone: _false())
    monkeypatch.setattr(consumer.rds, "push_buffer", lambda phone, text: _raise())

    msg = {
        "phone": "5511989887525",
        "msg_type": "Conversation",
        "msg": "Começar que horas ?",
        "from_me": False,
        "chat_id": "5511989887525@s.whatsapp.net",
        "message_id": "3EB0ABCDEF123456",
    }

    # 1a entrega: passa do dedup (chega no get_lead, que dispara nosso _raise
    # no push_buffer — entao paramos antes, mas ja confirmamos que passou).
    with pytest.raises(RuntimeError):
        await consumer._process_message(dict(msg))
    assert processed_after_dedup == ["5511989887525"]

    # 2a entrega (mesmo id): descartada no dedup, NAO chega no get_lead.
    await consumer._process_message(dict(msg))
    assert processed_after_dedup == ["5511989887525"], "duplicata nao deveria reprocessar"


async def _false():
    return False


def _raise():
    raise RuntimeError("nao deveria bufferizar nesta sentinela")
