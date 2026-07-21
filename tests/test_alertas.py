import logging

import pytest

from maestro.alertas import Alertas, de_ambiente


class _TG:
    """Dublê do TelegramClient: grava (chat, texto)."""
    def __init__(self):
        self.msgs = []

    def send_message(self, chat, texto):
        self.msgs.append((chat, texto))


class _TGQuebra:
    """Dublê que sempre levanta — modela o Telegram fora do ar."""
    def send_message(self, chat, texto):
        raise RuntimeError("telegram fora do ar")


def test_captura_morreu_envia_plataforma_e_motivo():
    tg = _TG()
    Alertas(tg, [10]).captura_morreu("Stoa", "processo sumiu há 10h")
    assert len(tg.msgs) == 1
    chat, texto = tg.msgs[0]
    assert chat == 10
    assert "Stoa" in texto and "processo sumiu há 10h" in texto
    assert "MORREU" in texto


def test_sessao_expirada_envia_plataforma():
    tg = _TG()
    Alertas(tg, [10]).sessao_expirada("Hotmart")
    assert len(tg.msgs) == 1
    assert "Hotmart" in tg.msgs[0][1] and "expirou" in tg.msgs[0][1]


def test_curso_concluido_envia_curso_e_n():
    tg = _TG()
    Alertas(tg, [10]).curso_concluido("Kiwify", "Seguro de Vida", 42)
    assert len(tg.msgs) == 1
    texto = tg.msgs[0][1]
    assert "Kiwify" in texto and "Seguro de Vida" in texto and "42" in texto


def test_cada_evento_tem_mensagem_distinta():
    tg = _TG()
    a = Alertas(tg, [1])
    a.captura_morreu("P", "m")
    a.sessao_expirada("P")
    a.curso_concluido("P", "C", 1)
    textos = [t for _, t in tg.msgs]
    assert len(set(textos)) == 3  # nenhum evento colide com outro


def test_envia_para_todos_os_chats():
    tg = _TG()
    Alertas(tg, [1, 2, 3]).captura_morreu("P", "m")
    assert [c for c, _ in tg.msgs] == [1, 2, 3]


def test_fail_safe_sem_cliente_so_loga(caplog):
    with caplog.at_level(logging.WARNING, logger="athena.alertas"):
        Alertas(None, [1]).captura_morreu("P", "morreu")
    assert any("só-log" in r.message for r in caplog.records)


def test_fail_safe_sem_chats_so_loga(caplog):
    tg = _TG()
    with caplog.at_level(logging.WARNING, logger="athena.alertas"):
        Alertas(tg, []).sessao_expirada("P")
    assert tg.msgs == []
    assert any("só-log" in r.message for r in caplog.records)


def test_erro_de_envio_nao_propaga(caplog):
    # Telegram fora do ar não pode derrubar quem chamou o alerta.
    with caplog.at_level(logging.ERROR, logger="athena.alertas"):
        Alertas(_TGQuebra(), [1, 2]).curso_concluido("P", "C", 3)
    assert sum("falha ao enviar" in r.message for r in caplog.records) == 2


def test_de_ambiente_sem_token_vira_so_log(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "7, 8")
    a = de_ambiente()
    assert a._tg is None          # modo só-log
    assert a._chats == [7, 8]     # chats ainda parseados


def test_de_ambiente_com_token_constroi_cliente(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "9")
    a = de_ambiente()
    assert a._tg is not None
    assert a._chats == [9]
