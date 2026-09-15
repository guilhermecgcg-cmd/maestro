"""RODADA 2, ITEM 6 — nenhuma variável de ambiente numérica derruba o daemon.

O ACHADO (anterior ao ramo): `ATHENA_BENCH_EXIT5_MIN="três"` e os `float(os.getenv(...))` do mesmo
arquivo (cooldowns, portão de carga, proxies de custo, heartbeat de sistema, batimento) levantam
ValueError no import ou no `main()`: um typo no launch.sh deixa a captura inteira fora do ar, e o
launchd fica relançando um processo que morre na largada.

AGORA: toda leitura numérica passa por `_env_int`/`_env_float` (ilegível, nan, inf -> padrão +
WARNING). Dois dentes: um import real por variável com lixo, e a varredura do FONTE — nenhum
`int(os.getenv(…))`/`float(os.getenv(…))` sobra, inclusive dentro do `main()`, e a lista abaixo
cobre TODAS as leituras feitas no import.
"""
import ast
import os
import subprocess
import sys

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTE = os.path.join(RAIZ, "maestro", "athena_local.py")

# (env, constante do módulo, padrão impresso)
LEITURAS_NO_IMPORT = [
    ("ATHENA_BENCH_EXIT5_MIN", "_BENCH_EXIT5_MIN", "3"),
    ("ATHENA_COOLDOWN_CONCLUIDO_S", "_COOLDOWN_SAIDA_LIMPA_S", "21600.0"),
    ("ATHENA_COOLDOWN_SEM_PENDENCIA_S", "_COOLDOWN_SEM_PENDENCIA_S", "86400.0"),
    ("ATHENA_CARGA_ALERTA_S", "_CARGA_ALERTA_S", "3600.0"),
    ("ATHENA_CARGA_EPISODIO_LACUNA_S", "_CARGA_EPISODIO_LACUNA_S", "1800.0"),
    ("ATHENA_BENCH_EXIT4_MIN", "_BENCH_EXIT4_MIN", "3"),
    ("ATHENA_ESPERA_EXIT4_S", "_ESPERA_EXIT4_S", "600.0"),
    ("ATHENA_BENCH_EXIT4_EXPIRA_S", "_BENCH_EXIT4_EXPIRA_S", "86400.0"),
    ("ATHENA_CUSTO_PROXY_CLAUDE_P_USD", "_CUSTO_PROXY_CLAUDE_P_USD", "0.02"),
    ("ATHENA_SISTEMA_HEARTBEAT_S", "_HEARTBEAT_LIMIAR_S", "900.0"),
    ("ATHENA_CUSTO_PROXY_BRAINSTORM_USD", "_CUSTO_PROXY_BRAINSTORM_USD", "0.05"),
]


@pytest.mark.parametrize("lixo", ["três", "inf"])
@pytest.mark.parametrize("env,constante,padrao", LEITURAS_NO_IMPORT,
                         ids=[e for e, _, _ in LEITURAS_NO_IMPORT])
def test_env_ilegivel_cai_no_padrao_sem_derrubar_o_import(env, constante, padrao, lixo):
    r = subprocess.run(
        [sys.executable, "-c", f"from maestro import athena_local as a; print(a.{constante})"],
        cwd=RAIZ, env=dict(os.environ, **{env: lixo}), capture_output=True, text=True,
        timeout=120)
    assert r.returncode == 0, r.stderr[-1200:]
    assert r.stdout.strip() == padrao, (r.stdout, r.stderr[-600:])
    assert env in r.stderr                                 # e avisa no log


@pytest.mark.parametrize("env,constante", [
    ("ATHENA_COOLDOWN_CONCLUIDO_S", "_COOLDOWN_SAIDA_LIMPA_S"),
    ("ATHENA_COOLDOWN_SEM_PENDENCIA_S", "_COOLDOWN_SEM_PENDENCIA_S"),
])
def test_cooldown_persistido_tem_teto_no_limite_de_leitura(env, constante):
    # os cooldowns também viram prazo em disco (`cooldown_ate`): acima do limite de leitura
    # ele seria descartado no primeiro reinício — mesmo defeito do item 3, mesmo teto
    r = subprocess.run(
        [sys.executable, "-c", f"from maestro import athena_local as a; print(a.{constante})"],
        cwd=RAIZ, env=dict(os.environ, **{env: "432000"}), capture_output=True, text=True,
        timeout=120)
    assert r.returncode == 0, r.stderr[-1200:]
    assert r.stdout.strip() == "172800.0", (r.stdout, r.stderr[-600:])


def _arvore():
    with open(FONTE, encoding="utf-8") as f:
        return ast.parse(f.read())


def _nome_da_env(no):
    """'X' se `no` é os.getenv("X", …) / os.environ.get("X", …); senão None."""
    if not isinstance(no, ast.Call) or not no.args or not isinstance(no.args[0], ast.Constant):
        return None
    f = no.func
    if isinstance(f, ast.Attribute) and f.attr == "getenv" and getattr(f.value, "id", "") == "os":
        return no.args[0].value
    if (isinstance(f, ast.Attribute) and f.attr == "get" and isinstance(f.value, ast.Attribute)
            and f.value.attr == "environ"):
        return no.args[0].value
    return None


def test_nenhuma_leitura_numerica_crua_de_env_sobra_no_arquivo():
    cruas = []
    for no in ast.walk(_arvore()):
        if (isinstance(no, ast.Call) and isinstance(no.func, ast.Name)
                and no.func.id in ("int", "float") and no.args):
            nome = _nome_da_env(no.args[0])
            if nome:
                cruas.append(f"{no.func.id}({nome}) linha {no.lineno}")
    assert cruas == [], cruas


def test_a_lista_cobre_todas_as_leituras_de_env_feitas_no_import():
    no_import = set()
    for no in _arvore().body:                              # só o nível do módulo (import)
        for sub in ast.walk(no):
            if isinstance(no, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                break
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                    and sub.func.id in ("_env_int", "_env_float", "int", "float")
                    and sub.args):
                arg = sub.args[0]
                nome = (arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                        else _nome_da_env(arg))
                if nome:
                    no_import.add(nome)
    assert no_import == {e for e, _, _ in LEITURAS_NO_IMPORT}, no_import
