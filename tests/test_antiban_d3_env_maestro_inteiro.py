"""D3, ITEM 6 (achado 5 da revisão independente) — nenhuma leitura numérica de env derruba o
daemon, em NENHUM módulo do `maestro/`.

O ACHADO: `maestro/config.py` fazia `intervalo_s=float(os.getenv("MAESTRO_INTERVALO_S", "120"))`
cru, e o `athena_local.main()` chama `carregar()` na partida: um typo no launch.sh derrubava o
daemon antes do primeiro ciclo — e o launchd ficava relançando um processo que morre na largada.
A varredura da rodada 2 só olhava `athena_local.py`.

AGORA: `carregar()` lê o intervalo de forma tolerante (ilegível, nan, inf -> 120 com WARNING;
mínimo 1 s), e a varredura por AST cobre TODO `maestro/`: um `int()`/`float()` de env fora de um
`try` que trate `ValueError` é defeito.
"""
import ast
import os
import pathlib

import pytest

RAIZ = pathlib.Path(__file__).resolve().parent.parent
MAESTRO = RAIZ / "maestro"


@pytest.mark.parametrize("bruto,esperado", [("dois minutos", 120.0), ("inf", 120.0),
                                            ("nan", 120.0), ("0", 1.0), ("", 120.0),
                                            (" 300 ", 300.0)])
def test_carregar_tolera_intervalo_ilegivel(monkeypatch, bruto, esperado):
    from maestro import config
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "MUTED")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "chave-de-teste")
    monkeypatch.setenv("MAESTRO_INTERVALO_S", bruto)
    assert config.carregar().intervalo_s == esperado


def _nome_da_env(no):
    if not isinstance(no, (ast.Call, ast.Subscript)):
        return None
    if isinstance(no, ast.Subscript):                      # os.environ["X"]
        v = no.value
        if isinstance(v, ast.Attribute) and v.attr == "environ":
            fatia = no.slice
            return fatia.value if isinstance(fatia, ast.Constant) else "?"
        return None
    if not no.args or not isinstance(no.args[0], ast.Constant):
        return None
    f = no.func
    if isinstance(f, ast.Attribute) and f.attr == "getenv" and getattr(f.value, "id", "") == "os":
        return no.args[0].value
    if (isinstance(f, ast.Attribute) and f.attr == "get" and isinstance(f.value, ast.Attribute)
            and f.value.attr == "environ"):
        return no.args[0].value
    return None


def _trata_valueerror(no_try):
    for h in no_try.handlers:
        tipos = h.type
        nomes = []
        if tipos is None:
            return True
        for t in (tipos.elts if isinstance(tipos, ast.Tuple) else [tipos]):
            nomes.append(getattr(t, "id", getattr(t, "attr", "")))
        if {"ValueError", "Exception", "BaseException"} & set(nomes):
            return True
    return False


def leituras_cruas(fonte, nome="<fonte>"):
    """`int()`/`float()` de env que NÃO estão dentro de um `try` que trata ValueError."""
    arvore = ast.parse(fonte)
    pais = {}
    for no in ast.walk(arvore):
        for filho in ast.iter_child_nodes(no):
            pais[filho] = no
    cruas = []
    for no in ast.walk(arvore):
        if not (isinstance(no, ast.Call) and isinstance(no.func, ast.Name)
                and no.func.id in ("int", "float") and no.args):
            continue
        env = _nome_da_env(no.args[0])
        if env is None:
            continue
        cur, protegido = no, False
        while cur in pais:
            cur = pais[cur]
            if isinstance(cur, ast.Try) and _trata_valueerror(cur):
                protegido = True
                break
        if not protegido:
            cruas.append(f"{nome}:{no.lineno} {no.func.id}({env})")
    return cruas


def test_o_varredor_pega_leitura_crua_e_aceita_a_protegida():
    assert leituras_cruas('import os\nX = float(os.getenv("A", "1"))\n') == ["<fonte>:2 float(A)"]
    assert leituras_cruas('import os\nX = int(os.environ["B"])\n') == ["<fonte>:2 int(B)"]
    protegida = ('import os\ntry:\n    X = float(os.getenv("A", "1"))\n'
                 'except (TypeError, ValueError):\n    X = 1.0\n')
    assert leituras_cruas(protegida) == []


def test_nenhuma_leitura_numerica_crua_de_env_em_todo_o_maestro():
    cruas = []
    for arq in sorted(MAESTRO.rglob("*.py")):
        cruas += leituras_cruas(arq.read_text(encoding="utf-8"), str(arq.relative_to(RAIZ)))
    assert cruas == [], cruas
