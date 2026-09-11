"""A PASSADA NÃO AGE SOBRE UMA MORTE QUE A AUTÓPSIA AINDA NÃO CLASSIFICOU (10/09).

MECANISMO (conferido em ~/.athena-local/decisoes/2026-09-10.jsonl, só leitura): o
`_reap` roda em qualquer consulta ao executor — inclusive no `curso_ativo` da própria
passada, que vem DEPOIS da autópsia do ciclo e de uma leitura do Notion de até 120s.
Colhida ali, a morte caía no FALLBACK da passada (conta 1 falha, fase NOVO) e a conta era
RE-DISPARADA na hora; no ciclo seguinte a autópsia classificava a MESMA morte e contava
OUTRA falha. Hubla 23:25→23:51: 3 mortes, 4 falhas no disjuntor (R1 contada 2x: fallback
~23:33 + autópsia 23:37). E o re-disparo vinha ANTES da causa: numa sessão morta (exit 3,
IRREDUTÍVEL) era mais um golpe na superfície de ban; numa saída limpa (exit 0) o fallback
contava falha e o cooldown de concluído nunca armava.

Correção: se a conta do curso tem um óbito colhido e ainda NÃO drenado pela autópsia, a
passada espera o próximo ciclo (quem decide é a autópsia: 1 falha, a causa certa). Os
dublês são o LocalExecutor REAL + vigia/causa/disjuntor REAIS; o motor morre DURANTE a
leitura do Notion (o ponto do incidente).
"""
import glob
import json

import pytest

from maestro import athena_local, causa, disjuntor
from maestro.adaptadores import captura
from tests.test_evidencia_reap import (_RUN_MORREU_DE_TIMEOUT, _RUN_SEGUINTE, _Alertas,
                                       _SpawnTee, _VigiaNoMundo, _Voz, _executor)

K = "https://dashboard.kiwify.com.br/courses"


def _cenario(tmp_path, saida_da_morte):
    sp = _SpawnTee([saida_da_morte, _RUN_SEGUINTE])
    cursos = [captura.CursoLocal(K, "kiwify-principal", "kiwify", total_esperado=40)]
    ex = _executor(tmp_path, cursos, sp)
    matar = {}

    def prog(curso):
        if curso in matar:                                 # morre ENQUANTO lê o Notion
            sp.calls[-1]["proc"].encerrar(matar.pop(curso))
        return (0, 40)

    alertas, voz, estado = _Alertas(), _Voz(), {}
    kw = dict(disjuntor=disjuntor, vigia=_VigiaNoMundo(sp.vivo), causa=causa,
              alertas=alertas, lock_dir=str(tmp_path / "locks"),
              autopsia_dir=str(tmp_path / "aut"), boot_ts=0.0)

    def ciclo(agora):
        athena_local.ciclo_local(cursos, ex, prog, voz, {}, estado, agora=agora, **kw)

    return sp, ex, matar, estado, alertas, ciclo


def test_morte_colhida_na_passada_conta_uma_falha_e_so_relanca_depois_da_causa(tmp_path):
    sp, ex, matar, estado, alertas, ciclo = _cenario(tmp_path, _RUN_MORREU_DE_TIMEOUT)
    ciclo(1000.0)
    assert len(sp.calls) == 1
    matar[K] = 1                                           # TimeoutError, exit 1
    ciclo(1200.0)
    # DENTES: antes -> o fallback contava 1 falha e re-disparava AQUI (2 spawns).
    assert len(sp.calls) == 1, "re-disparou antes de a autópsia classificar a morte"
    assert estado[K].get("disj_falhas", 0) == 0
    ciclo(1400.0)                                          # a autópsia classifica e decide
    assert estado[K]["ultima_causa"] == "relancar"
    # DENTES: antes -> 2 (fallback + autópsia contavam a MESMA morte).
    assert estado[K]["disj_falhas"] == 1
    assert len(sp.calls) == 2                              # relançado DEPOIS da causa


def test_sessao_morta_colhida_na_passada_nao_ganha_redisparo_antes_do_latch(tmp_path):
    # ANTI-BAN: exit 3 = sessão morta (irredutível). Re-disparar antes da autópsia era
    # mais uma sonda com a sessão morta na plataforma.
    sp, ex, matar, estado, alertas, ciclo = _cenario(
        tmp_path, "SESSÃO MORTA — LOGIN MANUAL NECESSÁRIO\n")
    ciclo(1000.0)
    matar[K] = 3
    ciclo(1200.0)
    ciclo(1400.0)
    assert estado[K].get("irredutivel") is True            # a autópsia latchou o reseed
    assert len(sp.calls) == 1                              # DENTES: antes eram 2 spawns
    assert ("SESSAO", {"essencial": True}) in alertas.mortes


def test_saida_limpa_colhida_na_passada_arma_cooldown_e_nao_conta_falha(tmp_path):
    sp, ex, matar, estado, alertas, ciclo = _cenario(
        tmp_path, "Stats: total=40 ok=0 audio=0 falhou=0\n")
    ciclo(1000.0)
    matar[K] = 0
    ciclo(1200.0)
    ciclo(1400.0)
    # DENTES: antes -> o fallback contava a saída limpa como FALHA e re-disparava sem
    # cooldown (o curso concluído virava disjuntor aberto + escalada crítica).
    assert estado[K].get("disj_falhas", 0) == 0
    assert estado[K].get("cooldown_ate", 0) > 1400.0
    assert len(sp.calls) == 1


def test_outro_curso_da_mesma_conta_espera_a_autopsia_e_o_err_sobrevive(tmp_path):
    # O cenário do incidente com 2 cursos na MESMA conta (hotmart-principal tem vários):
    # o 2º curso NÃO pega a conta antes da autópsia do 1º (nem trunca o .err dela), e a
    # autópsia classifica pelo TimeoutError. Depois dela, a conta segue (never-stop).
    c1 = "https://hotmart.com/pt-br/club/x/products/111"
    c2 = "https://hotmart.com/pt-br/club/y/products/222"
    sp = _SpawnTee([_RUN_MORREU_DE_TIMEOUT, _RUN_SEGUINTE])
    cursos = [captura.CursoLocal(c1, "hotmart-principal", "hotmart", total_esperado=18),
              captura.CursoLocal(c2, "hotmart-principal", "hotmart", total_esperado=18)]
    ex = _executor(tmp_path, cursos, sp)
    matar = {}

    def prog(curso):
        if curso in matar:
            sp.calls[-1]["proc"].encerrar(matar.pop(curso))
        return (0, 18)

    alertas, estado = _Alertas(), {}
    kw = dict(disjuntor=disjuntor, vigia=_VigiaNoMundo(sp.vivo), causa=causa,
              alertas=alertas, lock_dir=str(tmp_path / "locks"),
              autopsia_dir=str(tmp_path / "aut"), boot_ts=0.0)
    athena_local.ciclo_local(cursos, ex, prog, _Voz(), {}, estado, agora=1000.0, **kw)
    matar[c1] = 1
    athena_local.ciclo_local(cursos, ex, prog, _Voz(), {}, estado, agora=1200.0, **kw)
    assert len(sp.calls) == 1                              # DENTES: antes c1 era relançado
    assert "TimeoutError" in open(ex._stderr_path("hotmart-principal")).read()
    athena_local.ciclo_local(cursos, ex, prog, _Voz(), {}, estado, agora=1400.0, **kw)
    [aut] = [json.load(open(p)) for p in glob.glob(str(tmp_path / "aut" / "*.json"))]
    assert aut["acao"] == "relancar" and "TimeoutError" in aut["stderr_tail"], aut
    assert estado[c1]["disj_falhas"] == 1 and alertas.essenciais() == []
    assert len(sp.calls) == 2                              # a conta seguiu após a causa


def test_sem_vigia_o_fallback_ainda_conta_a_morte_no_ciclo_seguinte(tmp_path):
    # Degradado (vigia nulo): a drenagem do próximo ciclo libera a conta e o FALLBACK da
    # passada conta a falha — nada fica preso esperando uma autópsia que não vem.
    sp = _SpawnTee([_RUN_MORREU_DE_TIMEOUT, _RUN_SEGUINTE])
    cursos = [captura.CursoLocal(K, "k", "kiwify", total_esperado=40)]
    ex = _executor(tmp_path, cursos, sp)
    matar = {}

    def prog(curso):
        if curso in matar:
            sp.calls[-1]["proc"].encerrar(matar.pop(curso))
        return (0, 40)

    estado = {}

    def ciclo(agora):
        athena_local.ciclo_local(cursos, ex, prog, _Voz(), {}, estado, agora=agora,
                                 disjuntor=disjuntor)

    ciclo(1000.0)
    matar[K] = 1
    ciclo(1200.0)
    assert len(sp.calls) == 1
    ciclo(1400.0)
    assert estado[K]["disj_falhas"] == 1 and len(sp.calls) == 2


def test_aguardando_autopsia_e_por_conta_e_zera_na_drenagem(tmp_path):
    # O óbito é da CONTA (a unidade do anti-ban e do .err): outro curso da MESMA conta
    # também espera; drenar libera.
    sp = _SpawnTee([_RUN_MORREU_DE_TIMEOUT])
    c1 = "https://hotmart.com/pt-br/club/x/products/111"
    c2 = "https://hotmart.com/pt-br/club/y/products/222"
    c3 = "https://hotmart.com/pt-br/club/z/products/333"
    ex = _executor(tmp_path, [captura.CursoLocal(c1, "a", "hotmart"),
                              captura.CursoLocal(c2, "a", "hotmart"),
                              captura.CursoLocal(c3, "b", "hotmart")], sp)
    ex.disparar(c1)
    assert not ex.aguardando_autopsia(c1)                  # vivo: nada a esperar
    sp.calls[0]["proc"].encerrar(1)
    assert ex.aguardando_autopsia(c1) and ex.aguardando_autopsia(c2)
    assert not ex.aguardando_autopsia(c3)                  # outra conta segue livre
    ex.drenar_obitos()
    assert not ex.aguardando_autopsia(c1) and not ex.aguardando_autopsia(c2)


@pytest.mark.parametrize("quebrado", ["levanta", "ausente"])
def test_executor_sem_ou_com_sonda_quebrada_nao_trava_a_passada(quebrado):
    # fail-open: sonda ausente (dublês antigos) ou que levanta => comportamento de sempre.
    from tests.test_athena_local import FakeExecutor, FakeVoz, _curso, _prog
    ex = FakeExecutor({K: "a"})
    if quebrado == "levanta":
        def _boom(curso):
            raise RuntimeError("sonda quebrada")
        ex.aguardando_autopsia = _boom
    estado = {}
    athena_local.ciclo_local([_curso(K, total=40)], ex, _prog({K: (0, 40)}), FakeVoz(),
                             {}, estado, agora=1000.0)
    assert ex.disparos == [K]
