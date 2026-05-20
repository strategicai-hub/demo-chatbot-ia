"""
Whitelist `ALLOWED_PHONES`: se configurada, apenas numeros listados sao
processados. Vazia significa "aceita todos".
"""
import pytest

from app import consumer
from app.config import settings


@pytest.mark.asyncio
async def test_from_me_does_not_block_by_default(monkeypatch):
    settings.ALLOWED_PHONES = ""
    settings.BLOCK_ON_FROM_ME = False
    called = False

    async def fake_set_block(phone):
        nonlocal called
        called = True

    async def fake_is_bot_outbound(phone):
        return False

    monkeypatch.setattr(consumer.rds, "set_block", fake_set_block)
    monkeypatch.setattr(consumer.rds, "is_bot_outbound", fake_is_bot_outbound)

    await consumer._process_message({
        "phone": "5511999990000",
        "msg_type": "Conversation",
        "from_me": True,
        "chat_id": "5511999990000@c.us",
    })
    assert not called, "fromMe nao deve bloquear por padrao"
    settings.ALLOWED_PHONES = ""
    settings.BLOCK_ON_FROM_ME = False


@pytest.mark.asyncio
async def test_from_me_blocks_when_human_takeover_enabled(monkeypatch):
    settings.ALLOWED_PHONES = ""
    settings.BLOCK_ON_FROM_ME = True
    called = False

    async def fake_set_block(phone):
        nonlocal called
        called = True

    async def fake_is_bot_outbound(phone):
        return False

    monkeypatch.setattr(consumer.rds, "set_block", fake_set_block)
    monkeypatch.setattr(consumer.rds, "is_bot_outbound", fake_is_bot_outbound)

    await consumer._process_message({
        "phone": "5511999990000",
        "msg_type": "Conversation",
        "from_me": True,
        "chat_id": "5511999990000@c.us",
    })
    assert called, "fromMe so deve bloquear quando BLOCK_ON_FROM_ME=true"
    settings.BLOCK_ON_FROM_ME = False


@pytest.mark.asyncio
async def test_whitelist_blocks_non_listed_phone(monkeypatch):
    settings.ALLOWED_PHONES = "5511888880000"
    called = False

    async def fake_set_block(phone):
        nonlocal called
        called = True

    monkeypatch.setattr(consumer.rds, "set_block", fake_set_block)

    # Phone fora da whitelist; mesmo com from_me=True nao deve chegar ao set_block
    await consumer._process_message({
        "phone": "5511999990000",
        "msg_type": "Conversation",
        "from_me": True,
        "chat_id": "5511999990000@c.us",
    })
    assert not called, "Phone fora da whitelist deveria ter sido ignorado antes"

    # Reset
    settings.ALLOWED_PHONES = ""


@pytest.mark.asyncio
async def test_whitelist_allows_listed_phone(monkeypatch):
    settings.ALLOWED_PHONES = "5511888880000,5511999990000"
    settings.BLOCK_ON_FROM_ME = True
    called = False

    async def fake_set_block(phone):
        nonlocal called
        called = True

    async def fake_is_bot_outbound(phone):
        return False

    monkeypatch.setattr(consumer.rds, "set_block", fake_set_block)
    monkeypatch.setattr(consumer.rds, "is_bot_outbound", fake_is_bot_outbound)

    await consumer._process_message({
        "phone": "5511999990000",
        "msg_type": "Conversation",
        "from_me": True,
        "chat_id": "5511999990000@c.us",
    })
    assert called, "Phone listado deveria passar"

    settings.ALLOWED_PHONES = ""
    settings.BLOCK_ON_FROM_ME = False


@pytest.mark.asyncio
async def test_from_me_recent_bot_outbound_does_not_block(monkeypatch):
    settings.ALLOWED_PHONES = ""
    called = False

    async def fake_set_block(phone):
        nonlocal called
        called = True

    async def fake_is_bot_outbound(phone):
        return True

    monkeypatch.setattr(consumer.rds, "set_block", fake_set_block)
    monkeypatch.setattr(consumer.rds, "is_bot_outbound", fake_is_bot_outbound)

    await consumer._process_message({
        "phone": "5511999990000",
        "msg_type": "Conversation",
        "from_me": True,
        "chat_id": "5511999990000@c.us",
    })
    assert not called, "Eco de mensagem automatica do bot nao deveria bloquear o contato"


@pytest.mark.asyncio
async def test_reset_clears_block_before_block_check(monkeypatch):
    settings.ALLOWED_PHONES = ""
    settings.BLOCK_ON_FROM_ME = False
    calls = []

    async def fake_clear_chat_history(phone):
        calls.append(("clear_history", phone))

    async def fake_delete_lead(phone):
        calls.append(("delete_lead", phone))

    async def fake_delete_buffer(phone):
        calls.append(("delete_buffer", phone))

    async def fake_delete_block(phone):
        calls.append(("delete_block", phone))

    async def fake_is_blocked(phone):
        raise AssertionError("/reset deve ser processado antes da checagem de bloqueio")

    async def fake_send_text(phone, text, delay=4000):
        calls.append(("send_text", phone, text))
        return {}

    monkeypatch.setattr(consumer.rds, "clear_chat_history", fake_clear_chat_history)
    monkeypatch.setattr(consumer.rds, "delete_lead", fake_delete_lead)
    monkeypatch.setattr(consumer.rds, "delete_buffer", fake_delete_buffer)
    monkeypatch.setattr(consumer.rds, "delete_block", fake_delete_block)
    monkeypatch.setattr(consumer.rds, "is_blocked", fake_is_blocked)
    monkeypatch.setattr(consumer.uazapi, "send_text", fake_send_text)
    monkeypatch.setattr(consumer, "_save_session_log", lambda phone: None)

    await consumer._process_message({
        "phone": "5511999990000",
        "msg_type": "Conversation",
        "msg": "/reset",
        "from_me": False,
        "chat_id": "5511999990000@c.us",
    })

    assert ("delete_block", "5511999990000") in calls
    assert any(call[0] == "send_text" for call in calls)


def test_allowed_phones_set_trims_whitespace():
    settings.ALLOWED_PHONES = " 5511999990000 , 5511888880000 ,"
    assert settings.allowed_phones_set == {"5511999990000", "5511888880000"}
    settings.ALLOWED_PHONES = ""


def test_allowed_phones_set_empty_returns_empty_set():
    settings.ALLOWED_PHONES = ""
    assert settings.allowed_phones_set == set()
