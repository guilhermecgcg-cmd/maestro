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


# ==========================================================================
# ACHADO r6: a espera existia SÓ na passada. Um motor que morre DEPOIS do
# `_aguardando_autopsia(C2)` e ANTES do `_reap` do `disparar(C2)` — a janela inclui a
# leitura de pendência do tracker (SQLite, busy_timeout de até 5 s) e o disjuntor —
# deixava C2 disparar na MESMA conta antes da autópsia (a sonda do revisor: C1 sai com
# exit 3 durante o pendencia_fn(C2) e sai um 2º spawn na conta). Agora o `disparar`, o
# ponto único de todo disparo, recusa a conta com óbito colhido e não autopsiado.
# ==========================================================================
def test_morte_na_janela_da_pendencia_nao_deixa_outro_curso_da_conta_disparar(tmp_path):
    c1 = "https://hotmart.com/pt-br/club/x/products/111"
    c2 = "https://hotmart.com/pt-br/club/y/products/222"
    sp = _SpawnTee(["SESSÃO MORTA — LOGIN MANUAL NECESSÁRIO\n", ""])
    cursos = [captura.CursoLocal(c1, "hotmart-principal", "hotmart", total_esperado=18),
              captura.CursoLocal(c2, "hotmart-principal", "hotmart", total_esperado=18)]
    ex = _executor(tmp_path, cursos, sp)
    matar_na_pendencia = {}

    def pendencia(curso):
        # a leitura do tracker de C2 é lenta: C1 morre (exit 3) bem aqui — DEPOIS do
        # `_aguardando_autopsia(C2)` da passada (que viu C1 ainda vivo)
        alvo = matar_na_pendencia.pop(curso, None)
        if alvo is not None:
            sp.calls[0]["proc"].encerrar(alvo)
        return None

    alertas, estado = _Alertas(), {}
    kw = dict(disjuntor=disjuntor, vigia=_VigiaNoMundo(sp.vivo), causa=causa,
              alertas=alertas, lock_dir=str(tmp_path / "locks"),
              autopsia_dir=str(tmp_path / "aut"), boot_ts=0.0, pendencia_fn=pendencia)
    athena_local.ciclo_local(cursos, ex, lambda c: (0, 18), _Voz(), {}, estado,
                             agora=1000.0, **kw)
    assert len(sp.calls) == 1                              # C1 roda; C2 aguarda a conta
    matar_na_pendencia[c2] = 3
    athena_local.ciclo_local(cursos, ex, lambda c: (0, 18), _Voz(), {}, estado,
                             agora=1200.0, **kw)
    # DENTES: antes -> o `_reap` do disparar(C2) colhia C1 e soltava o lock: 2º spawn
    # na conta com a sessão morta, antes de a autópsia classificar a morte.
    assert len(sp.calls) == 1, "disparou na conta antes da autópsia da morte de C1"
    assert estado[c2].get("disj_falhas", 0) == 0           # esperar não é falha
    assert estado[c2].get("tentativas", 0) == 0
    athena_local.ciclo_local(cursos, ex, lambda c: (0, 18), _Voz(), {}, estado,
                             agora=1400.0, **kw)
    assert estado[c1].get("irredutivel") is True           # a autópsia latchou o reseed
    assert ("SESSAO", {"essencial": True}) in alertas.mortes
    # a evidência lida foi a da MORTE (o .err não foi truncado por um run de C2)
    [aut] = [json.load(open(p)) for p in glob.glob(str(tmp_path / "aut" / "*.json"))]
    assert "SESSÃO MORTA" in aut["stderr_tail"] and aut["curso"] == c1


def test_disparar_recusa_conta_com_obito_pendente_e_aceita_depois_da_drenagem(tmp_path):
    c1 = "https://hotmart.com/pt-br/club/x/products/111"
    c2 = "https://hotmart.com/pt-br/club/y/products/222"
    sp = _SpawnTee([_RUN_MORREU_DE_TIMEOUT, _RUN_SEGUINTE])
    ex = _executor(tmp_path, [captura.CursoLocal(c1, "a", "hotmart"),
                              captura.CursoLocal(c2, "a", "hotmart")], sp)
    ex.disparar(c1)
    sp.calls[0]["proc"].encerrar(1)                        # morre; NINGUÉM consultou ainda
    with pytest.raises(captura.AguardaAutopsia) as e:
        ex.disparar(c2)                                    # o `_reap` do próprio disparar
    assert "ainda não autopsiado" in e.value.args[0]
    with pytest.raises(captura.AguardaAutopsia):
        ex.disparar(c1)                                    # nem o mesmo curso relança
    assert len(sp.calls) == 1
    assert "TimeoutError" in open(ex._stderr_path("a")).read()   # .err intacto
    ex.drenar_obitos()
    assert ex.disparar(c2).startswith(f"local_iniciada:{c2}")


def test_aguarda_autopsia_vem_antes_do_portao_de_carga(tmp_path):
    # a causa de não disparar é a autópsia pendente — o log do adiamento não pode mentir
    # "máquina sobrecarregada" (nem a rampa contar esse não-disparo).
    from maestro import carga
    c1 = "https://hotmart.com/pt-br/club/x/products/111"
    sp = _SpawnTee([""])
    ex = _executor(tmp_path, [captura.CursoLocal(c1, "a", "hotmart")], sp)
    ex.disparar(c1)
    sp.calls[0]["proc"].encerrar(1)
    ex._portao_carga = carga.PortaoCarga(
        sensor=lambda: carga.LeituraCarga(24.0, 8, 4.0))
    with pytest.raises(captura.AguardaAutopsia) as e:
        ex.disparar(c1)
    assert not isinstance(e.value, captura.MaquinaSobrecarregada)


def test_ponta_a_ponta_spawn_real_recusa_a_conta_ate_a_autopsia(tmp_path):
    # SEM DUBLÊ no caminho do disparo: `_spawn_popen` REAL (subprocesso python, tee REAL,
    # lock REAL em disco, `poll()` REAL no reap). Motor de brinquedo num dir temporário
    # (nada de sessão/perfil/lock reais): o 1º run sai com exit 3 (sessão morta); o 2º
    # curso da MESMA conta não pode subir antes da autópsia.
    import sys
    import time
    motor = tmp_path / "motor"
    (motor / "motor").mkdir(parents=True)
    (motor / "motor" / "__init__.py").write_text("")
    (motor / "motor" / "kiwify.py").write_text(
        "import os, sys, time\n"
        "marca = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.ja_rodou')\n"
        "if not os.path.exists(marca):\n"
        "    open(marca, 'w').close()\n"
        "    sys.stderr.write('SESSÃO MORTA — LOGIN MANUAL NECESSÁRIO\\n')\n"
        "    sys.exit(3)\n"
        "sys.stdout.write('run 2 vivo\\n'); sys.stdout.flush()\n"
        "time.sleep(60)\n")
    k1 = "https://dashboard.kiwify.com.br/courses/a"
    k2 = "https://dashboard.kiwify.com.br/courses/b"
    cursos = [captura.CursoLocal(k1, "kiwify-principal", "kiwify"),
              captura.CursoLocal(k2, "kiwify-principal", "kiwify")]
    ex = captura.LocalExecutor(cursos, motor_python=sys.executable, motor_dir=str(motor),
                               lock_dir=str(tmp_path / "locks"),
                               motor_log_dir=str(tmp_path / "logs"))    # spawn REAL
    run2 = None
    try:
        ex.disparar(k1)
        p1 = ex._procs[k1]
        assert p1.wait(timeout=60) == 3                     # morreu de verdade (exit 3)
        with pytest.raises(captura.AguardaAutopsia):
            ex.disparar(k2)                                 # o reap REAL colheu k1 aqui
        assert k2 not in ex._procs                          # nenhum 2º processo subiu
        assert "SESSÃO MORTA" in open(ex._stderr_path("kiwify-principal")).read()
        obitos = ex.drenar_obitos()                         # a autópsia do ciclo
        assert obitos["kiwify-principal"]["exit_code"] == 3
        assert "SESSÃO MORTA" in obitos["kiwify-principal"]["stderr_tail"]
        assert ex.disparar(k2).startswith(f"local_iniciada:{k2}")
        run2 = ex._procs[k2]
        limite = time.time() + 30
        while run2.poll() is None and time.time() < limite and "run 2 vivo" not in open(
                ex._stderr_path("kiwify-principal")).read():
            time.sleep(0.05)
        assert run2.poll() is None                          # o 2º subiu depois da causa
    finally:
        # limpa QUALQUER processo que este executor subiu (inclusive o 2º indevido, se o
        # guard regredir e o teste falhar antes de chegar ao run2)
        for proc in list(ex._procs.values()) + ([run2] if run2 is not None else []):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
