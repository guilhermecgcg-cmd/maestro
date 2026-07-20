"""Testes-com-dentes do ORQUESTRADOR de criação de adaptadores (Capacidade B).

Não testam nenhum adaptador concreto — testam o PIPELINE que a Athena roda pra
nascer um adaptador de plataforma nova: recon -> Portão POP -> spec -> build
(subagente) -> review (Portão POP) -> deploy -> validação.

As travas que estes testes trancam:
  * o pipeline NÃO pula etapa (cada estágio exige a flag da etapa anterior);
  * Portão POP é OBRIGATÓRIO antes do spec E depois do build;
  * um review NÃO-clean BLOQUEIA o deploy (o deploy nem é chamado).
"""
from types import SimpleNamespace

import pytest

from maestro.adaptador_pipeline import (
    ORDEM,
    EstadoPipeline,
    pode_avancar,
    rodar_pipeline,
)


# --- dublês (seams) -------------------------------------------------------

class Spy:
    """Callable que registra as chamadas e devolve um resultado roteirizado."""

    def __init__(self, *resultados, retorno=None):
        self._fila = list(resultados)
        self._retorno = retorno
        self.chamadas = []

    def __call__(self, arg):
        self.chamadas.append(arg)
        if self._fila:
            return self._fila.pop(0)
        return self._retorno

    @property
    def n(self):
        return len(self.chamadas)


def ok(**kw):
    return SimpleNamespace(ok=True, **kw)


def falho(**kw):
    return SimpleNamespace(ok=False, **kw)


def clean():
    return SimpleNamespace(pronto=True, escalar=False, motivo="review limpo")


def sujo():
    return SimpleNamespace(pronto=False, escalar=True, motivo="achado bloqueante")


def seams_felizes():
    """Todos os estágios verdes e os DOIS portões limpos."""
    return dict(
        recon=Spy(retorno=ok(recon="dados")),
        portao=Spy(clean(), clean()),          # pré-spec, pós-build
        escrever_spec=Spy(retorno=ok(spec="texto")),
        build=Spy(retorno=ok(build="artefato")),
        deploy=Spy(retorno=ok(deploy="url")),
        validar=Spy(retorno=ok(validado="notion")),
    )


# --- estado / ordem / travas puras ---------------------------------------

def test_ordem_canonica_das_etapas():
    assert ORDEM == ("recon", "spec", "build", "review", "deploy", "validar")


def test_estado_inicial_tudo_falso():
    e = EstadoPipeline()
    assert not any(
        [e.recon_ok, e.spec_ok, e.build_ok, e.review_ok, e.deploy_ok, e.validado]
    )


@pytest.mark.parametrize(
    "etapa, flag_necessaria",
    [
        ("spec", "recon_ok"),
        ("build", "spec_ok"),
        ("review", "build_ok"),
        ("deploy", "review_ok"),
        ("validar", "deploy_ok"),
    ],
)
def test_pode_avancar_exige_a_flag_da_etapa_anterior(etapa, flag_necessaria):
    # Sem a flag anterior -> trancado.
    assert pode_avancar(EstadoPipeline(), etapa) is False
    # Só com a flag anterior -> liberado.
    assert pode_avancar(EstadoPipeline(**{flag_necessaria: True}), etapa) is True


def test_recon_sempre_pode_comecar():
    assert pode_avancar(EstadoPipeline(), "recon") is True


def test_deploy_exige_review_e_nao_apenas_build():
    """A trava central: build feito NÃO basta pra deployar; precisa do review_ok."""
    so_build = EstadoPipeline(recon_ok=True, spec_ok=True, build_ok=True)
    assert pode_avancar(so_build, "deploy") is False
    com_review = EstadoPipeline(
        recon_ok=True, spec_ok=True, build_ok=True, review_ok=True
    )
    assert pode_avancar(com_review, "deploy") is True


# --- pipeline ponta-a-ponta ----------------------------------------------

def test_pipeline_feliz_chega_a_validado():
    s = seams_felizes()
    r = rodar_pipeline("https://plataforma-nova.exemplo", **s)
    assert r.pronto is True
    assert r.escalar is False
    e = r.estado
    assert (e.recon_ok and e.spec_ok and e.build_ok
            and e.review_ok and e.deploy_ok and e.validado)
    # O Portão POP roda DUAS vezes: antes do spec e depois do build.
    assert s["portao"].n == 2


def test_recon_falho_nao_dispara_nada_downstream():
    s = seams_felizes()
    s["recon"] = Spy(retorno=falho(motivo="plataforma irreconhecível"))
    r = rodar_pipeline("url", **s)
    assert r.pronto is False and r.escalar is True
    assert r.parou_em == "recon"
    assert not r.estado.recon_ok
    # nada a jusante foi tocado — nem o Portão.
    assert s["portao"].n == 0
    assert s["escrever_spec"].n == 0
    assert s["build"].n == 0
    assert s["deploy"].n == 0
    assert s["validar"].n == 0


def test_portao_pop_obrigatorio_antes_do_spec():
    """Portão sujo ANTES do spec -> spec e tudo a jusante NÃO rodam."""
    s = seams_felizes()
    s["portao"] = Spy(sujo(), clean())  # 1ª chamada (pré-spec) reprova
    r = rodar_pipeline("url", **s)
    assert r.pronto is False and r.escalar is True
    assert r.parou_em == "portao_pre_spec"
    assert not r.estado.spec_ok
    assert s["escrever_spec"].n == 0
    assert s["build"].n == 0
    assert s["deploy"].n == 0
    # o portão foi chamado só a 1ª vez (a 2ª nem aconteceu).
    assert s["portao"].n == 1


def test_review_nao_clean_bloqueia_o_deploy():
    """DENTES: portão limpo pré-spec, SUJO no review (pós-build).
    O deploy não pode nem ser chamado."""
    s = seams_felizes()
    s["portao"] = Spy(clean(), sujo())  # pré-spec passa, review reprova
    r = rodar_pipeline("url", **s)
    assert r.pronto is False and r.escalar is True
    assert r.parou_em == "review"
    assert not r.estado.review_ok
    assert not r.estado.deploy_ok
    # build ACONTECEU, mas o deploy foi barrado.
    assert s["build"].n == 1
    assert s["deploy"].n == 0
    assert s["validar"].n == 0


def test_spec_falha_para_o_pipeline():
    s = seams_felizes()
    s["escrever_spec"] = Spy(retorno=falho(motivo="recon insuficiente"))
    r = rodar_pipeline("url", **s)
    assert r.parou_em == "spec" and r.escalar is True
    assert r.estado.recon_ok and not r.estado.spec_ok
    assert s["build"].n == 0
    # O Portão pré-spec passou (1x); o pós-build nem chegou.
    assert s["portao"].n == 1


def test_build_falha_nao_deploya():
    s = seams_felizes()
    s["build"] = Spy(retorno=falho(motivo="subagente não fechou os testes"))
    r = rodar_pipeline("url", **s)
    assert r.parou_em == "build" and r.escalar is True
    assert r.estado.spec_ok and not r.estado.build_ok
    # review (2º portão) e deploy não rodam.
    assert s["portao"].n == 1
    assert s["deploy"].n == 0


def test_validacao_falha_nao_declara_pronto():
    """I-1: sem prova na fonte de verdade, NÃO é pronto — mesmo com deploy ok."""
    s = seams_felizes()
    s["validar"] = Spy(retorno=falho(motivo="Notion não confirma as aulas"))
    r = rodar_pipeline("url", **s)
    assert r.parou_em == "validar"
    assert r.pronto is False and r.escalar is True
    assert r.estado.deploy_ok and not r.estado.validado


def test_ok_truthy_nao_booleano_nao_conta_como_sucesso():
    """DENTES fail-closed: um estágio que devolve `ok` truthy-mas-não-`True`
    (ex.: 1, "sim", um objeto de status) NÃO pode contar como sucesso. O gate
    exige `is True` — senão o pipeline avança 'no escuro'."""
    for valor in (1, "sim", ["x"], object()):
        s = seams_felizes()
        s["recon"] = Spy(retorno=SimpleNamespace(ok=valor, motivo="lixo truthy"))
        r = rodar_pipeline("url", **s)
        assert r.pronto is False and r.escalar is True
        assert r.parou_em == "recon"
        assert not r.estado.recon_ok
        # nada a jusante foi tocado — nem o Portão.
        assert s["portao"].n == 0
        assert s["escrever_spec"].n == 0


def test_portao_pronto_truthy_nao_booleano_nao_libera():
    """DENTES fail-closed: o Portão POP só libera com `pronto is True`. Um
    `pronto` truthy-não-booleano (pré-spec) BARRA — o spec não roda."""
    for valor in (1, "clean", object()):
        s = seams_felizes()
        s["portao"] = Spy(SimpleNamespace(pronto=valor), clean())
        r = rodar_pipeline("url", **s)
        assert r.pronto is False and r.escalar is True
        assert r.parou_em == "portao_pre_spec"
        assert not r.estado.spec_ok
        assert s["escrever_spec"].n == 0
        assert s["deploy"].n == 0


def test_review_pronto_truthy_nao_booleano_barra_o_deploy():
    """DENTES fail-closed: no review (pós-build), um `pronto` truthy-não-`True`
    NÃO pode liberar o deploy do adaptador numa plataforma real."""
    s = seams_felizes()
    s["portao"] = Spy(clean(), SimpleNamespace(pronto=1))  # review truthy-não-bool
    r = rodar_pipeline("url", **s)
    assert r.pronto is False and r.escalar is True
    assert r.parou_em == "review"
    assert not r.estado.review_ok
    assert not r.estado.deploy_ok
    assert s["build"].n == 1
    assert s["deploy"].n == 0


def test_trilha_registra_cada_etapa_executada_em_ordem():
    s = seams_felizes()
    r = rodar_pipeline("url", **s)
    etapas = [t.etapa for t in r.trilha]
    assert etapas == [
        "recon", "portao_pre_spec", "spec", "build", "review", "deploy", "validar"
    ]
    assert all(t.ok for t in r.trilha)
