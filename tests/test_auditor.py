from maestro.auditor import auditar, Criterio, contradicoes


def test_laudo_reprova_se_um_criterio_falha():
    cs = [Criterio("tela carrega", lambda ctx: (True, "200")),
          Criterio("nº de aulas real", lambda ctx: (False, "tela=586 mas postgres=0"))]
    laudo = auditar(cs, ctx={})
    assert laudo.aprovado is False
    assert any(not r.ok and "586" in r.evidencia for r in laudo.resultados)


def test_contradicao_e_falha_automatica():
    c = contradicoes({"tela_aulas": 586, "postgres_aulas": 0})
    assert c and "586" in c[0] and "0" in c[0]


def test_laudo_aprova_so_com_tudo_verde():
    cs = [Criterio("a", lambda ctx: (True, "")), Criterio("b", lambda ctx: (True, ""))]
    assert auditar(cs, ctx={}).aprovado is True


def test_criterio_que_estoura_e_falha():
    def _boom(ctx): raise RuntimeError("x")
    laudo = auditar([Criterio("z", _boom)], ctx={})
    assert laudo.aprovado is False and "erro ao verificar" in laudo.resultados[0].evidencia


def test_auditar_lista_vazia_reprova():
    # sem critério não há verificação -> NÃO aprova (fail-closed).
    assert auditar([], ctx={}).aprovado is False


# --- I-2: nunca sobrepor decisão de arquitetura do usuário ---
from maestro.auditor import criterio_igual


def test_criterio_igual_reprova_quando_real_diverge_da_decisao():
    # usuário decidiu projeto próprio; realidade veio dentro do conhecimento.
    c = criterio_igual("projeto easypanel", "maestro", "conhecimentoinfinito",
                       rotulo="projeto")
    laudo = auditar([c], ctx={})
    assert laudo.aprovado is False
    assert "viola I-2" in laudo.resultados[0].evidencia


def test_criterio_igual_aprova_quando_bate():
    c = criterio_igual("projeto easypanel", "maestro", "maestro", rotulo="projeto")
    assert auditar([c], ctx={}).aprovado is True


def test_contradicao_arquitetura_e_falha_automatica():
    c = contradicoes({"arquitetura_decidida": "projeto:maestro",
                      "arquitetura_real": "projeto:conhecimentoinfinito"})
    assert c and "I-2" in c[0]
