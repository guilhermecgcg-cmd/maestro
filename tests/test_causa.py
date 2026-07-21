"""CAUSA — classifica um Obito numa Decisao(acao) do conjunto FECHADO de ações.

Dublês COM DENTES: o seam do LLM é uma função INJETADA — os testes contam quantas vezes
foi chamada e o que devolveu; um LLM ausente/quebrado/fora-do-conjunto DEVE cair em
`escalar_humano` (fail-closed). A ação `relogar` (login automático) NUNCA pode sair de
lugar nenhum (inviolável anti-ban) — há teste explícito para isso.
"""
import pytest

from maestro import causa
from maestro.vigia import Obito


def _obito(stderr="", exit_code=1, conta="acme", curso="http://c", flaps=1):
    return Obito(conta=conta, curso=curso, exit_code=exit_code,
                 stderr_tail=stderr, flaps_na_janela=flaps, ts="")


# --------------------------------------------------------------------------
# assinaturas determinísticas
# --------------------------------------------------------------------------
@pytest.mark.parametrize("stderr", [
    "SessionLostError: session expired",
    "Please login again to continue",
    "sua sessão expirou, refaça o login",
    "ERROR: not logged in",
])
def test_sessao_escala_reseed(stderr):
    d = causa.classificar(_obito(stderr=stderr))
    assert d.acao == "escalar_reseed"


@pytest.mark.parametrize("stderr", [
    "GROQ_API_KEY invalid",
    "openai.AuthenticationError: HTTP 401 Unauthorized",
    "403 Forbidden: insufficient permissions",
    "Incorrect API key provided",
])
def test_api_escala_token(stderr):
    d = causa.classificar(_obito(stderr=stderr))
    assert d.acao == "escalar_token"


@pytest.mark.parametrize("exit_code,stderr", [
    (-9, ""),                       # SIGKILL
    (137, ""),                      # 128+9 (SIGKILL via shell)
    (1, "MemoryError"),
    (1, "Out of memory: Killed process 123"),
    (1, "asyncio.TimeoutError: operation timed out"),
])
def test_sigkill_oom_timeout_relanca(exit_code, stderr):
    d = causa.classificar(_obito(exit_code=exit_code, stderr=stderr))
    assert d.acao == "relancar"


@pytest.mark.parametrize("exit_code,stderr", [
    (0, ""),                                 # saída limpa (a completude é do loop/Notion)
    (1, "ConnectionResetError: connection reset by peer"),
    (1, "503 Service Unavailable, try again later"),
    (1, "temporarily unavailable"),
])
def test_transitorio_aguarda_backoff(exit_code, stderr):
    d = causa.classificar(_obito(exit_code=exit_code, stderr=stderr))
    assert d.acao == "aguardar_backoff"


def test_traceback_benigno_com_Session_nao_vira_reseed():
    # Um blip de rede (503) cujo traceback Python cita 'requests.sessions.Session' NÃO
    # pode ser confundido com sessão-morta — a causa-raiz é transitória (backoff).
    err = ('Traceback (most recent call last):\n'
           '  File "requests/sessions.py", line 700, in send\n'
           '    r = adapter.send(request, **kwargs)\n'
           '  <requests.sessions.Session object at 0x10a>\n'
           'HTTPError: 503 Service Unavailable\n')
    d = causa.classificar(_obito(stderr=err))
    assert d.acao == "aguardar_backoff", d


def test_reseed_tem_precedencia_sobre_relancar():
    # morreu com SIGKILL, mas o stderr denuncia sessão: a causa-raiz é a sessão.
    d = causa.classificar(_obito(exit_code=-9, stderr="session expired then killed"))
    assert d.acao == "escalar_reseed"


# --------------------------------------------------------------------------
# seam do LLM (desconhecida)
# --------------------------------------------------------------------------
def test_desconhecida_consulta_llm_uma_vez():
    chamadas = []
    def llm(prompt):
        chamadas.append(prompt)
        return "relancar"
    d = causa.classificar(_obito(stderr="erro totalmente inédito xyzzy 9f3a"), llm=llm)
    assert d.acao == "relancar"
    assert d.fonte == "llm"
    assert len(chamadas) == 1                    # EXATAMENTE uma chamada


def test_llm_ausente_fail_closed_humano():
    d = causa.classificar(_obito(stderr="erro totalmente inédito xyzzy 9f3a"), llm=None)
    assert d.acao == "escalar_humano"


def test_llm_resposta_fora_do_conjunto_fail_closed():
    d = causa.classificar(_obito(stderr="erro inédito xyzzy"),
                          llm=lambda p: "faça um café e reinicie o roteador")
    assert d.acao == "escalar_humano"


def test_llm_excecao_fail_closed():
    def llm(p):
        raise RuntimeError("claude indisponível")
    d = causa.classificar(_obito(stderr="erro inédito xyzzy"), llm=llm)
    assert d.acao == "escalar_humano"


def test_llm_pode_devolver_acao_valida_do_conjunto():
    for acao in ("aguardar_backoff", "escalar_reseed", "escalar_token", "escalar_humano"):
        d = causa.classificar(_obito(stderr="inédito %s" % acao),
                              llm=lambda p, a=acao: '{"acao": "%s"}' % a)
        assert d.acao == acao


# --------------------------------------------------------------------------
# INVIOLÁVEL: nunca propor relogar (login automático)
# --------------------------------------------------------------------------
def test_llm_nunca_propoe_relogar():
    d = causa.classificar(_obito(stderr="inédito"),
                          llm=lambda p: "relogar")
    assert d.acao == "escalar_humano"           # 'relogar' é RECUSADO, vira humano
    assert d.acao != "relogar"


def test_relogar_nao_esta_no_conjunto_fechado():
    assert "relogar" not in causa.ACOES
    assert "login" not in causa.ACOES
    # o conjunto fechado é exatamente estes:
    assert causa.ACOES == {
        "escalar_reseed", "escalar_token", "relancar",
        "aguardar_backoff", "escalar_humano",
    }
