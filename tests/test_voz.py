from maestro.voz import Voz, Comando
from maestro.playbook import Acao
from maestro.sentinela import Problema


class _TG:
    def __init__(self): self.msgs = []
    def send_message(self, chat, texto): self.msgs.append((chat, texto))


def test_avisa_acao_executada():
    tg = _TG(); v = Voz(tg, [1, 2])
    v.avisar_acao(Acao("reiniciei o worker", True, False))
    assert len(tg.msgs) == 2 and "reiniciei o worker" in tg.msgs[0][1]


def test_nao_avisa_acao_nao_executada():
    tg = _TG(); Voz(tg, [1]).avisar_acao(Acao("", False, True, "x"))
    assert tg.msgs == []


def test_escala_com_pedido():
    tg = _TG(); v = Voz(tg, [1])
    v.escalar(Problema("job_falhou", "3", "", "aviso"), "job 3 falhou")
    assert "job 3 falhou" in tg.msgs[0][1]


def test_interpreta_comando_captura():
    c = Voz(_TG(), [1]).interpretar("captura https://hotmart.com/club/x/products/9")
    assert c == Comando("captura", "https://hotmart.com/club/x/products/9")


def test_interpreta_status():
    assert Voz(_TG(), [1]).interpretar("status") == Comando("status", "")
