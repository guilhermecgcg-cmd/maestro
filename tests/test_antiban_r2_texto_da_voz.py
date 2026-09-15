"""RODADA 2, ITEM 5 — a voz do bench de exit 4 diz a classe e o prazo, não "só o humano destrava".

O ACHADO: no nível `mudo` a única mensagem que sai para o dono é a da Voz
("captura_local_esgotada"), e o texto dizia "irredutível (reseed/token) só o humano destrava".
Para o bench de exit 4 isso é falso: ele vence sozinho no prazo, e o dono seria mandado agir
sem precisar.

AGORA: com o bench de exit 4, a Voz diz a classe (parede da plataforma do curso), a hora em que
o bench vence e que ele reabre sozinho para UMA tentativa. As demais aberturas do disjuntor
seguem com o texto de sempre.
"""
from datetime import datetime

from tests.test_antiban_exit4_classe_e_expiracao import _ABORT_LOCAL, _abortar
from tests.test_antiban_relancamento_exit4 import (_ABORT_EXIT4, VIRAL, _Daemon,
                                                   _sem_tracker_vivo)  # noqa: F401 (autouse)


def _pedidos(d):
    return [pedido for p, pedido in d.voz.escaladas
            if getattr(p, "tipo", None) == "captura_local_esgotada" and VIRAL in pedido]


def test_a_voz_do_bench_exit4_diz_classe_e_prazo_e_nao_manda_o_humano_destravar(tmp_path):
    d = _Daemon(tmp_path)
    _abortar(d, _ABORT_EXIT4, 3)
    prazo = d.st["benched_exit4_ate"]
    [texto] = _pedidos(d)
    assert "parede da plataforma do curso" in texto, texto
    assert datetime.fromtimestamp(prazo).strftime("%d/%m %H:%M") in texto, texto
    assert "só o humano destrava" not in texto, texto


def test_a_voz_de_escada_comum_continua_com_o_texto_de_sempre(tmp_path):
    d = _Daemon(tmp_path)
    _abortar(d, _ABORT_LOCAL, 3)                           # 3 falhas: a escada fecha, sem bench
    assert not d.st.get("benched_exit4")
    textos = _pedidos(d)
    assert textos and "DISJUNTOR ABERTO" in textos[-1], textos
