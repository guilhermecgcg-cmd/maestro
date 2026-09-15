"""D3, ITEM 8 — o guarded restart do daemon enxerga todo motor e não fica preso a lock de prova manual.

O ACHADO (`~/.athena-local/ativar_guarded_restart.sh`, copiado para `ops/`):
  - `motores_vivos` só casava `python -m motor\\.cli`: um `motor.entregadigital`, `motor.kiwify`,
    `motor.instagram` (ou o `python3.14 -m motor.x` do venv) rodando era invisível, e o restart
    podia acontecer com captura em andamento;
  - QUALQUER lock no diretório segurava a janela: o lock de uma prova MANUAL (a sonda p102, o
    `motor.cli --anexos`/`--retentar`), que o restart do daemon não afeta, segurava para sempre.

AGORA:
  - motor vivo = `[Pp]ython…` com opções do interpretador antes do `-m motor.` (qualquer braço,
    qualquer nome de interpretador, o `Python` de framework do macOS — D3r2, A3);
  - um lock só é ACEITO (não segura) quando o dono é explicitamente MANUAL (`sonda-p102`,
    `motor-anexos`, `motor-retentar`, `manual…`) E o PID dele não descende do daemon; o motor
    desse lock (e o que descende dele) também não segura. Lock sem dono, ilegível, de dono
    manual mas filho do daemon, ou de PID desconhecido: segura (conservador).

Dublês: `ps`, `launchctl`, `pgrep` e `sleep` de mentira no PATH (tabela de processos num arquivo)
e o modo `AGR_SO_GUARDA=1`, que decide a janela e sai ANTES de qualquer unload/load. Nada toca o
launchd de verdade.
"""
import json
import os
import pathlib
import subprocess
import time

import pytest

RAIZ = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = RAIZ / "ops" / "ativar_guarded_restart.sh"
PID_DAEMON = 35810

_SHIM_PS = """#!/usr/bin/env python3
import os, sys
with open(os.environ["AGR_TESTE_PROCS"]) as f:
    for linha in f:
        if linha.strip():
            print(linha.rstrip("\\n"))
"""
_SHIM_PGREP = """#!/usr/bin/env python3
import os, re, sys
padrao = sys.argv[-1]
with open(os.environ["AGR_TESTE_PROCS"]) as f:
    for linha in f:
        partes = linha.strip().split(None, 2)
        if len(partes) == 3 and re.search(padrao, partes[2]):
            print(partes[0])
"""
_SHIM_LAUNCHCTL = """#!/usr/bin/env python3
import os, sys
if sys.argv[1:2] == ["list"]:
    print("PID\\tStatus\\tLabel")
    print(os.environ["AGR_TESTE_DAEMON"] + "\\t0\\tcom.athena.local")
else:
    with open(os.environ["AGR_TESTE_CHAMADAS"], "a") as f:
        f.write(" ".join(sys.argv[1:]) + "\\n")
"""
_SHIM_SLEEP = "#!/bin/sh\nexit 0\n"


def _rodar(tmp_path, procs, locks):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for nome, fonte in (("ps", _SHIM_PS), ("pgrep", _SHIM_PGREP), ("launchctl", _SHIM_LAUNCHCTL),
                        ("sleep", _SHIM_SLEEP)):
        p = bin_dir / nome
        p.write_text(fonte)
        p.chmod(0o755)
    tabela = [f"{PID_DAEMON} 1 /Users/g/teste/maestro/.venv/bin/python -m maestro.athena_local"]
    tabela += [f"{pid} {ppid} {args}" for pid, ppid, args in procs]
    (tmp_path / "procs.txt").write_text("\n".join(tabela) + "\n")
    locks_dir = tmp_path / "locks"
    locks_dir.mkdir()
    for i, dados in enumerate(locks):
        texto = dados if isinstance(dados, str) else json.dumps(dados)
        (locks_dir / f"conta{i}.lock").write_text(texto)
    pulso = tmp_path / "pulso.json"
    pulso.write_text(json.dumps({"ts": time.time(), "ciclo": 7, "ativos": []}))
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path),
        "AGR_PULSO": str(pulso), "AGR_LOCKS": str(locks_dir),
        "AGR_PLIST": str(tmp_path / "com.athena.local.plist"),
        "AGR_MAX_ITERS": "2", "AGR_SLEEP_S": "0", "AGR_SO_GUARDA": "1",
        "AGR_TESTE_PROCS": str(tmp_path / "procs.txt"), "AGR_TESTE_DAEMON": str(PID_DAEMON),
        "AGR_TESTE_CHAMADAS": str(tmp_path / "chamadas.txt"),
    }
    r = subprocess.run(["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True,
                       timeout=60)
    chamadas = (tmp_path / "chamadas.txt")
    assert not chamadas.exists() or "load" not in chamadas.read_text(), \
        "o modo só-guarda chamou unload/load"
    return r.stdout + r.stderr


def _resultado(saida):
    linhas = [l for l in saida.splitlines() if l.startswith("RESULTADO=")]
    assert linhas, saida[-1500:]
    return linhas[-1]


def test_motor_de_qualquer_braco_segura_a_janela(tmp_path):
    procs = [(41001, PID_DAEMON,
              "/Users/g/teste/aula/.venv/bin/python3.14 -m motor.entregadigital "
              "https://luanacarolina.entregadigital.app.br/")]
    assert _resultado(_rodar(tmp_path, procs, [])) == "RESULTADO=HELD", \
        "motor.entregadigital rodando e o guard deu a janela"


# D3r2, A3 (revisão independente): `python[^ ]* -m motor\.` não via o motor com opção do
# interpretador antes do `-m` (`python -u -m motor.cli`) nem o Python de framework do macOS
# (argv[0] "Python"). Os dois primeiros casos são os testes do revisor, copiados.
@pytest.mark.parametrize("args", [
    "/Users/g/teste/aula/.venv/bin/python -u -m motor.cli "
    "https://hotmart.com/pt-br/club/x/products/1",
    "/Library/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/"
    "Python -m motor.memberkit https://x",
    "/Users/g/teste/aula/.venv/bin/python3.14 -X dev -W ignore::DeprecationWarning "
    "-m motor.kiwify https://x",
    "/opt/homebrew/bin/python3.14t -B -m motor.instagram https://x",
    "/Users/g/teste/aula/.venv/bin/python -mmotor.cli https://x",
], ids=["opcao-u", "python-de-framework", "opcoes-com-argumento", "free-threaded", "m-colado"])
def test_motor_com_opcao_do_interpretador_ou_python_de_framework_segura_a_janela(tmp_path, args):
    assert _resultado(_rodar(tmp_path, [(41010, PID_DAEMON, args)], [])) == "RESULTADO=HELD", args


@pytest.mark.parametrize("args", [
    "/usr/bin/python3 -m http.server 8000",
    "/Users/g/teste/aula/.venv/bin/python -m pytest tests/test_motor.py",
    "/Users/g/teste/aula/.venv/bin/python -c import motor.cli",
    "/Users/g/teste/aula/.venv/bin/python " + "-B -X dev " * 40 + "-c pass",
], ids=["outro-modulo", "pytest-com-motor-no-caminho", "motor-sem-dash-m",
        "muitas-opcoes-sem-motor"])
def test_python_que_nao_roda_motor_nao_segura_a_janela(tmp_path, args):
    assert _resultado(_rodar(tmp_path, [(41020, 700, args)], [])) == "RESULTADO=GUARDA_OK", args


@pytest.mark.parametrize("dono", ["sonda-p102", "motor-anexos", "motor-retentar",
                                  "manual-guilherme"])
def test_lock_de_prova_manual_fora_do_daemon_nao_segura_para_sempre(tmp_path, dono):
    procs = [(52001, 700, "/Users/g/teste/aula/.venv/bin/python -m motor.cli --anexos "
                          "https://hotmart.com/pt-br/club/x/products/1")]
    locks = [{"pid": 52001, "course_url": "motor-anexos:https://hotmart.com/x", "conta":
              "hotmart-principal", "ts": time.time(), "dono": dono}]
    assert _resultado(_rodar(tmp_path, procs, locks)) == "RESULTADO=GUARDA_OK", \
        "a prova manual fora do daemon segurou o restart"


@pytest.mark.parametrize("lock,procs", [
    ({"pid": 43001, "course_url": "https://hotmart.com/x", "conta": "hotmart-principal",
      "ts": 1.0},
     [(43001, PID_DAEMON, "/v/python -m motor.cli https://hotmart.com/x")]),
    ({"pid": 43002, "course_url": "motor-anexos:x", "conta": "c", "ts": 1.0,
      "dono": "motor-anexos"},
     [(43002, PID_DAEMON, "/v/python -m motor.cli --anexos x")]),
    ('{"pid": 43003, "dono": "sonda-p1', []),
    ({"pid": None, "course_url": "x", "conta": "c", "ts": 1.0, "dono": "sonda-p102"}, []),
], ids=["lock-do-daemon", "dono-manual-mas-filho-do-daemon", "lock-ilegivel",
        "lock-manual-sem-pid"])
def test_os_demais_locks_seguram_a_janela(tmp_path, lock, procs):
    assert _resultado(_rodar(tmp_path, procs, [lock])) == "RESULTADO=HELD"


def test_sem_motor_e_sem_lock_a_janela_abre(tmp_path):
    assert _resultado(_rodar(tmp_path, [], [])) == "RESULTADO=GUARDA_OK"
