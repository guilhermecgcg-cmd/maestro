"""CADÊNCIA DO MOTOR DISPARADO PELO DAEMON — trilha anti-ban aprovada pelo dono (15/09).

O ACHADO (F1): o daemon dispara `python -m motor.cli <url>` (Hotmart) e `motor.<plataforma>`
nas demais, e NENHUM desses caminhos passava pacer ao `run_course`; nem o ambiente do daemon
nem o `aula/.env` definem TEXT_CONCURRENCY, então o semáforo ficava em 4 — quatro páginas de
aula abertas ao mesmo tempo na conta paga, cada uma logo depois da outra.

O CONTRATO que o daemon passa a cravar em TODO disparo (`LocalExecutor._montar`):
  - TEXT_CONCURRENCY = ATHENA_MOTOR_CONCORRENCIA (default 1, teto 2) — VENCE o valor
    herdado do ambiente/extra_env (o `aula/.env` é sourced pelo launch.sh);
  - CAPTURE_PACING_MIN_S / CAPTURE_PACING_JITTER_S com PISO 3 s / 2 s (os defaults do
    `motor.pacing`): herdar 0 não desliga a cadência; herdar mais que o piso vale.

Dublês com dentes: FakeSpawn registra o env de cada disparo. Nada de subprocesso real.
"""
import os

import pytest

from maestro.adaptadores import captura
from tests.test_adaptadores_cademi_entregadigital import LUANA, NOVAS, VIRAL
from tests.test_executor_local import (FakeSpawn, KAJABI, MK, STOA, STOA_DIR, C1, _exec,
                                       _hot, _kajabi, _stoa)

_SESS = {plat_url: sess for plat, plat_url, _c, _u, _s, sess in NOVAS}


def _cursos():
    return {
        "hotmart": (_hot(C1), C1, {}),
        "memberkit": (captura.CursoLocal(url=MK, conta="mk", plataforma="memberkit"), MK, {}),
        "cademi": (captura.CursoLocal(url=VIRAL, conta="cademi-codigoviral",
                                      plataforma="cademi", session_path=_SESS[VIRAL]),
                   VIRAL, {}),
        "entregadigital": (captura.CursoLocal(url=LUANA, conta="entregadigital-luanacarolina",
                                              plataforma="entregadigital",
                                              session_path=_SESS[LUANA]), LUANA, {}),
        "stoa": (_stoa(), STOA, {"motor_dir_por_plataforma": {"stoa": STOA_DIR}}),
        "kajabi": (_kajabi(), KAJABI, {}),
    }


def _env_do_disparo(plat, **kw):
    curso, url, extra = _cursos()[plat]
    sp = FakeSpawn()
    conf = _exec(curso, spawn=sp, **extra, **kw).disparar(url)
    assert conf and sp.calls, (plat, conf)
    return sp.calls[0]["env"]


@pytest.fixture(autouse=True)
def _ambiente_herdado_perigoso(monkeypatch):
    # o pior caso do launch.sh: o `aula/.env` descomentado com os valores de fábrica do
    # motor/uma cadência desligada. O daemon tem de cravar o contrato por cima disso.
    monkeypatch.setenv("TEXT_CONCURRENCY", "4")
    monkeypatch.setenv("CAPTURE_PACING_MIN_S", "0")
    monkeypatch.setenv("CAPTURE_PACING_JITTER_S", "0")
    monkeypatch.delenv("ATHENA_MOTOR_CONCORRENCIA", raising=False)


@pytest.mark.parametrize("plat", ["hotmart", "memberkit", "cademi", "entregadigital",
                                  "stoa", "kajabi"])
def test_todo_motor_disparado_leva_concorrencia_1_e_a_cadencia_com_piso(plat):
    env = _env_do_disparo(plat)
    assert env.get("TEXT_CONCURRENCY") == "1", (plat, env.get("TEXT_CONCURRENCY"))
    assert float(env["CAPTURE_PACING_MIN_S"]) >= 3.0, env["CAPTURE_PACING_MIN_S"]
    assert float(env["CAPTURE_PACING_JITTER_S"]) >= 2.0, env["CAPTURE_PACING_JITTER_S"]


def test_extra_env_do_daemon_nao_devolve_a_concorrencia_4():
    env = _env_do_disparo("cademi", extra_env={"TEXT_CONCURRENCY": "4",
                                               "CAPTURE_PACING_MIN_S": "0.5"})
    assert env["TEXT_CONCURRENCY"] == "1"
    assert float(env["CAPTURE_PACING_MIN_S"]) >= 3.0


@pytest.mark.parametrize("bruto,esperado", [("2", "2"), ("8", "2"), ("0", "1"), ("-3", "1"),
                                            ("abc", "1"), ("", "1"), (" 2 ", "2")])
def test_o_botao_de_concorrencia_do_daemon_tem_teto_2(monkeypatch, bruto, esperado):
    monkeypatch.setenv("ATHENA_MOTOR_CONCORRENCIA", bruto)
    assert _env_do_disparo("hotmart")["TEXT_CONCURRENCY"] == esperado


@pytest.mark.parametrize("bruto,esperado", [("10", 10.0), ("3.5", 3.5), ("nan", 3.0),
                                            ("inf", 3.0), ("lixo", 3.0)])
def test_cadencia_herdada_acima_do_piso_vale_e_lixo_cai_no_piso(monkeypatch, bruto, esperado):
    monkeypatch.setenv("CAPTURE_PACING_MIN_S", bruto)
    assert float(_env_do_disparo("kajabi")["CAPTURE_PACING_MIN_S"]) == esperado


# Contrato com o CÓDIGO do motor: o env que o daemon crava é o que os CLIs LEEM. Lê o fonte
# (texto, sem importar) da primeira árvore do motor que tiver o arquivo; sem árvore, pula.
_ARVORES = [p for p in (os.environ.get("ATHENA_MOTOR_DIR"),
                        "/Users/guilhermerodrigues/teste/aula-antiban",
                        "/Users/guilhermerodrigues/teste/aula") if p]
# a Stoa roda de OUTRA árvore do motor (ATHENA_MOTOR_DIR_STOA no launch.sh)
_ARVORES_STOA = [p for p in (os.environ.get("ATHENA_MOTOR_DIR_STOA"),
                             "/Users/guilhermerodrigues/teste/aula-2b/.claude/worktrees/"
                             "adaptador-stoa") if p]


@pytest.mark.parametrize("modulo", sorted({s.modulo for s in captura._PLATAFORMAS.values()}))
def test_o_cli_de_cada_modulo_disparado_le_TEXT_CONCURRENCY(modulo):
    rel = ("motor/config.py" if modulo == "motor.cli"
           else os.path.join(*modulo.split("."), "cli.py"))
    for raiz in (_ARVORES_STOA if modulo == "motor.stoa" else _ARVORES):
        caminho = os.path.join(raiz, rel)
        if os.path.isfile(caminho):
            with open(caminho, encoding="utf-8") as f:
                assert 'os.getenv("TEXT_CONCURRENCY"' in f.read(), caminho
            return
    pytest.skip(f"nenhuma árvore do motor com {rel} nesta máquina")
