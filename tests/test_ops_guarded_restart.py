"""D3, ITEM 8 — o guarded restart do daemon enxerga todo motor e não fica preso a lock de prova manual.

O ACHADO (`~/.athena-local/ativar_guarded_restart.sh`, copiado para `ops/`):
  - `motores_vivos` só casava `python -m motor\\.cli`: um `motor.entregadigital`, `motor.kiwify`,
    `motor.instagram` (ou o `python3.14 -m motor.x` do venv) rodando era invisível, e o restart
    podia acontecer com captura em andamento;
  - QUALQUER lock no diretório segurava a janela: o lock de uma prova MANUAL (a sonda p102, o
    `motor.cli --anexos`/`--retentar`), que o restart do daemon não afeta, segurava para sempre.

AGORA:
  - motor vivo = um token com "python" (sem diferenciar maiúsculas) e depois `-m` + `motor.…`
    (qualquer braço, qualquer opção do interpretador, o `Python` de framework do macOS — D3r2, A3;
    varredura linear dos tokens, a regra do contador da outra sessão — D3r3);
  - um lock só é ACEITO (não segura) quando o dono é explicitamente MANUAL (`sonda-p102`,
    `motor-anexos`, `motor-retentar`, `manual…`) E o PID dele não descende do daemon — vivo ou já
    fora do `ps` (D3r2, A4); o motor desse lock (e o que descende dele) também não segura. Lock sem
    dono, ilegível, de dono manual mas filho do daemon, ou sem PID: segura (conservador). Um motor
    vivo que NÃO descende do PID aceito (o órfão da prova manual morta) segura por ser motor.

Dublês: `ps`, `launchctl`, `pgrep` e `sleep` de mentira no PATH (tabela de processos num arquivo)
e o modo `AGR_SO_GUARDA=1`, que decide a janela e sai ANTES de qualquer unload/load. Nada toca o
launchd de verdade.
"""
import json
import os
import pathlib
import subprocess
import sys
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
# D3r3 (revisão independente do D3r2, achado 2): a expressão regular do A3 ainda perdia
# `-Xfrozen_modules=off`, `-Wignore::DeprecationWarning` e `--check-hash-based-pycs never`, e tinha
# backtracking exponencial (36 `-X` = 9 s). Virou `e_motor`, uma varredura linear dos tokens.
_LINHAS_DE_MOTOR = {
    "opcao-u": "/Users/g/teste/aula/.venv/bin/python -u -m motor.cli "
               "https://hotmart.com/pt-br/club/x/products/1",
    "python-de-framework": "/Library/Frameworks/Python.framework/Versions/3.14/Resources/"
                           "Python.app/Contents/MacOS/Python -m motor.memberkit https://x",
    "opcoes-com-argumento": "/Users/g/teste/aula/.venv/bin/python3.14 -X dev -W "
                            "ignore::DeprecationWarning -m motor.kiwify https://x",
    "free-threaded": "/opt/homebrew/bin/python3.14t -B -m motor.instagram https://x",
    "m-colado": "/Users/g/teste/aula/.venv/bin/python -mmotor.cli https://x",
    "X-colado-com-igual": "/Users/g/teste/aula/.venv/bin/python3.14 -Xfrozen_modules=off "
                          "-m motor.cli https://x",
    "W-colado": "/Users/g/teste/aula/.venv/bin/python -Wignore::DeprecationWarning "
                "-m motor.hubla https://x",
    "opcao-longa-com-valor": "/Users/g/teste/aula/.venv/bin/python --check-hash-based-pycs never "
                             "-m motor.greenn https://x",
    "caminho-com-espaco": "/Users/g/Library/Application Support/uv/python3.14 -m motor.cli "
                          "https://x",
}
_LINHAS_SEM_MOTOR = {
    "outro-modulo": "/usr/bin/python3 -m http.server 8000",
    "pytest-com-motor-no-caminho": "/Users/g/teste/aula/.venv/bin/python -m pytest "
                                   "tests/test_motor.py",
    "motor-sem-dash-m": "/Users/g/teste/aula/.venv/bin/python -c import motor.cli",
    "muitas-opcoes-sem-motor": "/Users/g/teste/aula/.venv/bin/python " + "-B -X dev " * 40
                               + "-c pass",
    "motor-sem-python": "/usr/local/bin/pypy3 -m motor.cli https://x",
}
# as duas folgas documentadas em `e_motor` sobre a regra do contador da outra sessão
_FOLGAS = {"m-colado", "caminho-com-espaco"}


@pytest.mark.parametrize("args", list(_LINHAS_DE_MOTOR.values()), ids=list(_LINHAS_DE_MOTOR))
def test_motor_com_opcao_do_interpretador_ou_python_de_framework_segura_a_janela(tmp_path, args):
    assert _resultado(_rodar(tmp_path, [(41010, PID_DAEMON, args)], [])) == "RESULTADO=HELD", args


@pytest.mark.parametrize("args", list(_LINHAS_SEM_MOTOR.values()), ids=list(_LINHAS_SEM_MOTOR))
def test_python_que_nao_roda_motor_nao_segura_a_janela(tmp_path, args):
    assert _resultado(_rodar(tmp_path, [(41020, 700, args)], [])) == "RESULTADO=GUARDA_OK", args


# A checagem de motor do script, isolada do `ps`: o `e_motor` do heredoc (ou, no script de antes, a
# expressão regular `MOTOR`) — para medir o tempo e comparar com a regra da outra sessão.
_CHECAGEM = r'''
import ast, re, sys, time
fonte = open(sys.argv[1], encoding="utf-8").read()
codigo = fonte.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
nos = [n for n in ast.parse(codigo).body
       if (isinstance(n, ast.FunctionDef) and n.name == "e_motor")
       or (isinstance(n, ast.Assign) and any(getattr(a, "id", "") == "MOTOR" for a in n.targets))]
ns = {"re": re}
exec(compile(ast.Module(body=nos, type_ignores=[]), "bloqueios", "exec"), ns)
checar = ns.get("e_motor") or (lambda a: bool(ns["MOTOR"].search(a)))
for linha in sys.argv[2:]:
    t0 = time.perf_counter()
    casou = bool(checar(linha))
    print(int(casou), f"{(time.perf_counter() - t0) * 1000:.3f}")
'''


def _checar(*linhas, timeout=10):
    r = subprocess.run([sys.executable, "-c", _CHECAGEM, str(SCRIPT), *linhas],
                       capture_output=True, text=True, timeout=timeout)
    assert r.returncode == 0, r.stderr[-800:]
    return [(bool(int(c)), float(ms)) for c, ms in (l.split() for l in r.stdout.splitlines())]


@pytest.mark.parametrize("linha,casa", [
    ("/Users/g/teste/aula/.venv/bin/python " + "-X " * 100 + "-c pass", False),
    ("/Users/g/teste/aula/.venv/bin/python " + "-X dev " * 50 + "-m motor.cli https://x", True),
], ids=["100-X-sem-motor", "100-opcoes-com-motor"])
def test_a_checagem_de_motor_leva_menos_de_100ms_numa_linha_de_100_opcoes(linha, casa):
    try:
        [(casou, ms)] = _checar(linha)
    except subprocess.TimeoutExpired:
        pytest.fail("a checagem de motor passou de 10 s numa linha de 100 opções (backtracking)")
    assert casou is casa and ms < 100.0, (casou, ms)


def _regra_do_contador_da_outra_sessao(args):
    """A regra de `motores_aula.py` (sessão 3adef9ce), copiada: argv[0] com "python" (sem
    diferenciar maiúsculas) e um token `-m` seguido de um token que começa com `motor.`."""
    t = args.split()
    return bool(t and "python" in t[0].lower()
                and any(t[i] == "-m" and t[i + 1].startswith("motor.") for i in range(len(t) - 1)))


def test_a_checagem_de_motor_segue_a_regra_do_contador_da_outra_sessao():
    linhas = {**_LINHAS_DE_MOTOR, **_LINHAS_SEM_MOTOR}
    resultado = dict(zip(linhas, (casou for casou, _ms in _checar(*linhas.values()))))
    for nome, linha in linhas.items():
        outra = _regra_do_contador_da_outra_sessao(linha)
        if outra:
            assert resultado[nome], f"{nome}: o contador da outra sessão conta e o restart não segura"
        if resultado[nome] and not outra:
            assert nome in _FOLGAS, f"{nome}: casou além da regra sem ser uma folga documentada"
    assert {n for n in linhas if resultado[n]} == set(_LINHAS_DE_MOTOR), resultado


@pytest.mark.parametrize("dono", ["sonda-p102", "motor-anexos", "motor-retentar",
                                  "manual-guilherme"])
def test_lock_de_prova_manual_fora_do_daemon_nao_segura_para_sempre(tmp_path, dono):
    procs = [(52001, 700, "/Users/g/teste/aula/.venv/bin/python -m motor.cli --anexos "
                          "https://hotmart.com/pt-br/club/x/products/1")]
    locks = [{"pid": 52001, "course_url": "motor-anexos:https://hotmart.com/x", "conta":
              "hotmart-principal", "ts": time.time(), "dono": dono}]
    assert _resultado(_rodar(tmp_path, procs, locks)) == "RESULTADO=GUARDA_OK", \
        "a prova manual fora do daemon segurou o restart"


# D3r2, A4 (revisão independente): o cabeçalho dizia "lock de PID ausente: segura"; o código aceita o
# lock manual cujo PID não está no `ps`. Alinhado o CABEÇALHO ao código, não o contrário (o porquê
# está no cabeçalho do script) — o teste do revisor, que pedia HELD, NÃO foi copiado. O que protege
# um motor em voo é a tabela de processos: o segundo teste prova que o órfão vivo da prova manual
# morta ainda segura a janela.
def test_lock_manual_de_pid_que_ja_morreu_nao_segura_a_janela(tmp_path):
    locks = [{"pid": 99991, "course_url": "motor-anexos:x", "conta": "c", "ts": time.time(),
              "dono": "motor-anexos"}]
    assert _resultado(_rodar(tmp_path, [], locks)) == "RESULTADO=GUARDA_OK"


def test_motor_orfao_da_prova_manual_morta_ainda_segura_a_janela(tmp_path):
    locks = [{"pid": 99991, "course_url": "motor-anexos:x", "conta": "c", "ts": time.time(),
              "dono": "motor-anexos"}]
    procs = [(52010, 1, "/Users/g/teste/aula/.venv/bin/python -m motor.cli --anexos "
                        "https://hotmart.com/pt-br/club/x/products/1")]
    assert _resultado(_rodar(tmp_path, procs, locks)) == "RESULTADO=HELD"


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
