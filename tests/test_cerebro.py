from maestro.cerebro import diagnosticar
from maestro.sentinela import Problema


def _diag(tipo, alvo, log, llm):
    return diagnosticar(Problema(tipo, alvo, "", "critico"), log, llm)


def test_llm_sugere_restart_seguro():
    llm = lambda p: '{"diagnostico":"OOM no painel-api","acao":"restart","escalar":false}'
    d = _diag("servico_restart_loop", "painel-api", "log...", llm)
    assert d.acao == "restart" and not d.escalar


def test_acao_perigosa_do_llm_vira_escalar():
    llm = lambda p: '{"diagnostico":"x","acao":"login","escalar":false}'
    d = _diag("x", "y", "log", llm)
    assert d.escalar and d.acao == "nada"   # ação fora da whitelist é rejeitada


def test_json_ilegivel_escala():
    d = _diag("x", "y", "log", lambda p: "não é json")
    assert d.escalar


def test_json_valido_nao_objeto_escala():
    d = _diag("x", "y", "log", lambda p: "\"nada\"")   # JSON válido mas não-dict
    assert d.escalar and d.acao == "nada"
