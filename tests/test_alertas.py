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


def test_captura_morreu_essencial_envia_plataforma_e_motivo():
    # `essencial=True`: morte que exige ação humana (contrato pós-gate 22/07 —
    # o default sem `essencial` é só-log, testado na seção do gate abaixo).
    tg = _TG()
    Alertas(tg, [10]).captura_morreu("Stoa", "processo sumiu há 10h", essencial=True)
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
    a.captura_morreu("P", "m", essencial=True)
    a.sessao_expirada("P")
    a.curso_concluido("P", "C", 1)
    textos = [t for _, t in tg.msgs]
    assert len(set(textos)) == 3  # nenhum evento colide com outro


def test_envia_para_todos_os_chats():
    tg = _TG()
    Alertas(tg, [1, 2, 3]).captura_morreu("P", "m", essencial=True)
    assert [c for c, _ in tg.msgs] == [1, 2, 3]


def test_fail_safe_sem_cliente_so_loga(caplog):
    with caplog.at_level(logging.WARNING, logger="athena.alertas"):
        Alertas(None, [1]).captura_morreu("P", "morreu", essencial=True)
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


# ===========================================================================
# GATE DE ESSENCIALIDADE — só o que exige AÇÃO HUMANA (ou incidente real) pinga
# o Telegram; o resto vira SÓ-LOG (observabilidade intacta, canal limpo).
# ===========================================================================
def test_morte_nao_essencial_e_so_log_sem_telegram(caplog):
    # FLAP/relancar: o sistema auto-trata — NÃO pinga o dono (o bench exit-5, que PARA o
    # curso, é essencial desde a r6 — tests/test_bench_essencial.py).
    tg = _TG()
    with caplog.at_level(logging.WARNING, logger="athena.alertas"):
        Alertas(tg, [10]).captura_morreu(
            "hotmart", "FLAP: 3 mortes na janela (curso X, causa=relancar)")
    assert tg.msgs == []                                   # canal Telegram: silêncio
    assert any("FLAP" in r.message for r in caplog.records)  # log: registra TUDO


def test_morte_essencial_envia():
    tg = _TG()
    Alertas(tg, [10]).captura_morreu(
        "hotmart", "causa desconhecida (fail-closed)", essencial=True)
    assert len(tg.msgs) == 1
    assert "MORREU" in tg.msgs[0][1]


def test_sessao_expirada_sempre_envia_mesmo_no_nivel_essencial():
    # INVARIANTE anti-ban: reseed é o ÚNICO jeito de o dono saber que precisa logar.
    tg = _TG()
    Alertas(tg, [10]).sessao_expirada("hotmart")
    assert len(tg.msgs) == 1 and "expirou" in tg.msgs[0][1]


def test_nivel_tudo_restaura_comportamento_antigo():
    tg = _TG()
    Alertas(tg, [10], nivel="tudo").captura_morreu("P", "FLAP: barulho")
    assert len(tg.msgs) == 1


def test_nivel_invalido_cai_em_essencial():
    tg = _TG()
    Alertas(tg, [10], nivel="barulhento").captura_morreu("P", "flap qualquer")
    assert tg.msgs == []                                   # fail-safe: corta o ruído


def test_de_ambiente_le_nivel_do_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "7")
    monkeypatch.setenv("ATHENA_ALERTA_NIVEL", "tudo")
    assert de_ambiente()._nivel == "tudo"
    monkeypatch.delenv("ATHENA_ALERTA_NIVEL")
    assert de_ambiente()._nivel == "essencial"             # default = essencial


# ===========================================================================
# DEDUP — o MESMO alerta essencial do MESMO curso não repete dentro da janela.
# ===========================================================================
def test_dedup_mesma_chave_na_janela_envia_uma_vez(caplog):
    tg = _TG()
    t = [1000.0]
    a = Alertas(tg, [10], relogio=lambda: t[0])
    a.captura_morreu("hotmart", "causa desconhecida", essencial=True,
                     chave=("humano", "curso-1"))
    t[0] += 60.0                                           # 1 min depois, mesma janela
    with caplog.at_level(logging.WARNING, logger="athena.alertas"):
        a.captura_morreu("hotmart", "causa desconhecida", essencial=True,
                         chave=("humano", "curso-1"))
    assert len(tg.msgs) == 1                               # 2 escaladas -> 1 envio
    assert any("dedup" in r.message for r in caplog.records)  # a 2ª foi pro log


def test_dedup_expira_fora_da_janela():
    tg = _TG()
    t = [1000.0]
    a = Alertas(tg, [10], dedup_janela_s=3600.0, relogio=lambda: t[0])
    a.captura_morreu("P", "m", essencial=True, chave=("humano", "c"))
    t[0] += 3601.0                                         # janela venceu
    a.captura_morreu("P", "m", essencial=True, chave=("humano", "c"))
    assert len(tg.msgs) == 2                               # incidente NOVO re-alerta


def test_dedup_chaves_diferentes_nao_colidem():
    tg = _TG()
    a = Alertas(tg, [10], relogio=lambda: 1000.0)
    a.captura_morreu("P", "m", essencial=True, chave=("humano", "curso-1"))
    a.captura_morreu("P", "m", essencial=True, chave=("humano", "curso-2"))
    assert len(tg.msgs) == 2                               # cursos distintos: ambos


def test_sem_chave_nao_dedupa():
    tg = _TG()
    a = Alertas(tg, [10], relogio=lambda: 1000.0)
    a.captura_morreu("P", "m", essencial=True)
    a.captura_morreu("P", "m", essencial=True)
    assert len(tg.msgs) == 2                               # sem chave = sem dedup


def test_sessao_expirada_nunca_dedupa():
    # Fail-safe da INVARIANTE: reseed repetido ainda avisa (nunca engolir reseed).
    tg = _TG()
    a = Alertas(tg, [10], relogio=lambda: 1000.0)
    a.sessao_expirada("hotmart")
    a.sessao_expirada("hotmart")
    assert len(tg.msgs) == 2


# ===========================================================================
# Endurecimentos do review (IMPORTANTE-1/2): dedup só conta envio que CHEGOU,
# e nenhum bug de dedup pode derrubar o chamador nem engolir alerta.
# ===========================================================================
class _TGFalhaUmaVez:
    """Dublê: 1ª chamada levanta (Telegram fora do ar), depois entrega."""
    def __init__(self):
        self.msgs = []
        self._falhou = False

    def send_message(self, chat, texto):
        if not self._falhou:
            self._falhou = True
            raise RuntimeError("telegram fora do ar")
        self.msgs.append((chat, texto))


def test_envio_falho_nao_marca_dedup_e_proxima_tentativa_envia():
    # IMPORTANTE-1: se o 1º envio essencial FALHOU, a mesma chave na janela NÃO
    # pode ser dedupada — senão o dono fica 1h sem saber de uma escalada.
    tg = _TGFalhaUmaVez()
    a = Alertas(tg, [10], relogio=lambda: 1000.0)
    a.captura_morreu("P", "m", essencial=True, chave=("humano", "c"))   # falha
    a.captura_morreu("P", "m", essencial=True, chave=("humano", "c"))   # retenta
    assert len(tg.msgs) == 1                               # a 2ª CHEGOU (não dedupada)


def test_chave_nao_hashavel_nao_derruba_e_envia():
    # IMPORTANTE-2: fail-safe total — bug de chave jamais levanta pro loop nem
    # engole o alerta (na dúvida, ENVIA).
    tg = _TG()
    a = Alertas(tg, [10], relogio=lambda: 1000.0)
    a.captura_morreu("P", "m", essencial=True, chave=["lista", "nao-hashavel"])
    a.captura_morreu("P", "m", essencial=True, chave=["lista", "nao-hashavel"])
    assert len(tg.msgs) == 2                               # nunca levantou, sempre enviou


def test_nivel_tudo_tambem_desliga_dedup():
    # 'tudo' promete o comportamento ANTIGO por inteiro (debug): sem gate E sem dedup.
    tg = _TG()
    a = Alertas(tg, [10], nivel="tudo", relogio=lambda: 1000.0)
    a.captura_morreu("P", "m", essencial=True, chave=("humano", "c"))
    a.captura_morreu("P", "m", essencial=True, chave=("humano", "c"))
    a.curso_concluido("P", "C", 1)
    a.curso_concluido("P", "C", 1)
    assert len(tg.msgs) == 4


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
