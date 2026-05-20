from app.consumer import _parse_ai_response


def test_text_simple():
    parts, fin, trans = _parse_ai_response("Oi, tudo bem?")
    assert parts == [{"type": "text", "content": "Oi, tudo bem?"}]
    assert fin is False
    assert trans is False


def test_finalizado_flag_true():
    parts, fin, trans = _parse_ai_response("Tchau! [FINALIZADO=1]")
    assert parts[0]["content"] == "Tchau!"
    assert fin is True
    assert trans is False


def test_finalizado_flag_false():
    _, fin, _ = _parse_ai_response("Ainda conversando [FINALIZADO=0]")
    assert fin is False


def test_transferir_flag_true():
    parts, fin, trans = _parse_ai_response(
        "Excelente! Vou repassar para a equipe. [TRANSFERIR=1]"
    )
    assert trans is True
    assert fin is False
    assert "[TRANSFERIR=1]" not in parts[0]["content"]


def test_both_flags_together():
    parts, fin, trans = _parse_ai_response("Combinado! [TRANSFERIR=1] [FINALIZADO=1]")
    assert fin is True
    assert trans is True
    assert "[TRANSFERIR" not in parts[0]["content"]
    assert "[FINALIZADO" not in parts[0]["content"]


def test_split_by_triple_pipe():
    parts, _, _ = _parse_ai_response("Oi!|||Tudo bem?")
    assert len(parts) == 2
    assert parts[0]["content"] == "Oi!"
    assert parts[1]["content"] == "Tudo bem?"


def test_split_by_double_newline():
    parts, _, _ = _parse_ai_response("Primeira.\n\nSegunda.")
    assert len(parts) == 2


def test_event_reminder_farewell_is_moved_to_last_part():
    parts, _, _ = _parse_ai_response(
        "A Comunicação Humanizada ajuda a transformar conversas difíceis.\n\n"
        "Vou te enviar lembretes mais próximo do dia do evento. "
        "E, se tiver qualquer dúvida até lá, pode me perguntar por aqui.\n\n"
        "A propósito, temos uma oportunidade especial: "
        "o livro físico com dedicatória exclusiva do Eduardo Almeida."
    )

    assert len(parts) == 3
    assert parts[1]["content"].startswith("A propósito")
    assert parts[-1]["content"].startswith("Vou te enviar lembretes")


def test_new_event_closing_messages_are_moved_to_last_parts():
    parts, _, _ = _parse_ai_response(
        "Comunicação Humanizada ajuda a reduzir conflitos.\n\n"
        "Quando estivermos mais perto da data, eu vou te mandar novos lembretes "
        "por aqui para você não esquecer, ta bom?\n\n"
        "A propósito, para quem se inscreveu agora, temos uma oportunidade especial: "
        "o livro físico com dedicatória exclusiva do Eduardo Almeida.\n\n"
        "Até lá, se você tiver qualquer dúvida, é só me chamar 😉"
    )

    assert len(parts) == 4
    assert parts[1]["content"].startswith("A propósito")
    assert parts[2]["content"].startswith("Quando estivermos")
    assert parts[3]["content"].startswith("Até lá")


def test_unknown_tag_falls_through_as_text():
    # Tag que nao esta em MEDIA_DICT deve virar texto normal
    parts, _, _ = _parse_ai_response("Olha isso: [FOTO_INEXISTENTE]")
    assert parts[0]["type"] == "text"
    assert "[FOTO_INEXISTENTE]" in parts[0]["content"]
