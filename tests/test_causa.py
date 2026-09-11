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
# CONTRATO DE EXIT-CODE dos CLIs do motor (uniforme em TODAS as plataformas):
#   2 = SessionLostError (sessão morreu no meio) -> reseed
#   3 = SessionDeadError/NavigationDeadError (morta no startup) -> reseed
#   4 = CircuitBreakerError (EXCESSO DE FALHAS): causa DESCONHECIDA por
#       construção. A mensagem LISTA "a sessão" como uma HIPÓTESE entre várias
#       (anti-bot, rede, detector ruim) — NÃO a declara morta. Classificar como
#       reseed aqui é o falso "Sessão expirou" que benchava o curso de vez.
# --------------------------------------------------------------------------

# Mensagem REAL do motor.cli no abort por excesso de falhas (SystemExit(4)).
# Contém a palavra "sessão" DENTRO de uma lista de hipóteses — o gatilho do bug.
_ABORT_CIRCUIT_BREAKER = (
    "⛔ RUN INTERROMPIDO POR EXCESSO DE FALHAS: 2 aulas processadas e NENHUMA "
    "funcionou (nem com legenda, nem sem). Algo está sistematicamente errado do "
    "lado da Hotmart — pode ser o anti-bot, a sessão, a rede, ou um detector "
    "nosso ruim. Continuar martelando é o caminho do banimento. Parando com "
    "ok=0 audio=0 falhou=2 de 2.\n"
    "3. SÓ SE for anti-bot: espere algumas horas e NÃO tente logar de novo"
)


def test_circuit_breaker_exit4_nao_vira_reseed_falso():
    # A sessão está VIVA; o circuit-breaker abortou por falhas de causa desconhecida.
    # NUNCA pode virar escalar_reseed (falso "expirou" + bench permanente do curso).
    d = causa.classificar(_obito(exit_code=4, stderr=_ABORT_CIRCUIT_BREAKER))
    assert d.acao != "escalar_reseed", d
    assert d.acao == "escalar_humano", d       # honesto: humano olha o tracker
    assert d.fonte == "deterministico", d      # decidido pelo exit-code, sem LLM


# Cauda de stderr REAL do incidente Stoa (21/07 21:12): um abort de circuit-breaker
# (exit 4) cujo tail contém a linha httpx de um POST BEM-SUCEDIDO à Groq (200 OK). A
# regex antiga tinha `groq` como âncora NUA e casava esta linha de sucesso -> classificava
# "troque o token" (escalar_token -> IRREDUTÍVEL) -> a captura latchava de vez, com as
# chaves VÁLIDAS (Groq e Anthropic testadas ao vivo = 200). O nome do provedor num log
# de sucesso NÃO é sinal de credencial ruim.
_ABORT_COM_LOG_GROQ_200 = (
    "2026-07-21 21:10:19,728 INFO httpx: HTTP Request: POST "
    "https://api.groq.com/openai/v1/audio/transcriptions \"HTTP/1.1 200 OK\"\n"
    "2026-07-21 21:10:42,426 INFO httpx: HTTP Request: POST "
    "https://api.anthropic.com/v1/messages \"HTTP/1.1 200 OK\"\n"
    "2026-07-21 21:11:08,359 ERROR motor.orchestrator: RUN ABORTADO com 1/7 aulas "
    "concluídas: 5 erros nas últimas 6 aulas. Algo está sistematicamente errado do "
    "lado da Hotmart — pode ser o anti-bot, a sessão, a rede, ou um detector nosso "
    "ruim. Continuar martelando é o caminho do banimento. Parando com ok=1 audio=0 "
    "falhou=5 de 7.\n"
    "As aulas não processadas continuam pendentes. Olhe os erros no tracker antes de "
    "retomar; se for anti-bot, espere e NÃO force novo login."
)


def test_log_de_sucesso_groq_200_NAO_vira_token_falso():
    # DENTES (incidente Stoa 21/07): o nome "groq" numa linha httpx de SUCESSO 200
    # jamais pode classificar como escalar_token (que é IRREDUTÍVEL -> latch de 1h28
    # com chaves válidas). exit 4 = causa desconhecida -> escalar_humano (retentável).
    d = causa.classificar(_obito(exit_code=4, stderr=_ABORT_COM_LOG_GROQ_200))
    assert d.acao != "escalar_token", d        # o bug: casava "groq" no log 200
    assert d.acao == "escalar_humano", d       # exit 4 honesto: humano olha o tracker


def test_provedor_mencionado_sem_falha_nao_vira_token():
    # Guarda geral: mencionar o provedor (Groq/Anthropic/OpenAI) sem um sinal de FALHA
    # de credencial não pode escalar troca de token. Aqui um SIGKILL (-9) transitório.
    d = causa.classificar(_obito(exit_code=-9, stderr="usando api.groq.com e api.anthropic.com"))
    assert d.acao != "escalar_token", d
    assert d.acao == "relancar", d             # SIGKILL -> transitório de recurso/SO


@pytest.mark.parametrize("exit_code,stderr", [
    (2, "SESSÃO MORREU NO MEIO DO RUN: redirecionado pro login ao abrir a aula: http://x"),
    (3, "SESSÃO MORTA: Login manual não concluído dentro do timeout"),
    (3, "NÃO CONSEGUI LER O ÍNDICE DO CURSO: /v1/navigation não devolveu o curso"),
])
def test_exit_code_sessao_morta_escala_reseed(exit_code, stderr):
    # Detecção REAL de sessão morta: o contrato de exit-code é autoritativo.
    d = causa.classificar(_obito(exit_code=exit_code, stderr=stderr))
    assert d.acao == "escalar_reseed", d
    assert d.fonte == "deterministico", d


@pytest.mark.parametrize("stderr", [
    "SESSÃO MEMBERKIT MORTA — LOGIN MANUAL NECESSÁRIO",
    "sessão morreu no meio do run",
    "redirecionado pro login ao abrir a aula",
    "Sessão Kajabi morta e re-seed não permitido",
    "faça o login manual quando ele pedir",
])
def test_sessao_morta_assertiva_pt_escala_reseed(stderr):
    # Frases ASSERTIVAS de sessão morta que o motor imprime — reseed mesmo com
    # exit_code genérico (defende quando o processo foi morto antes de sair).
    d = causa.classificar(_obito(exit_code=1, stderr=stderr))
    assert d.acao == "escalar_reseed", d


@pytest.mark.parametrize("stderr", [
    "sessão viva (state)",                        # log NORMAL do motor no startup
    "sessão persistida em .hotmart-session.json (164 cookies)",
    "sessão pronta (origem=state)",
])
def test_log_benigno_de_sessao_viva_nao_vira_reseed(stderr):
    # O motor loga "sessão viva/persistida/pronta" em operação NORMAL. Um exit
    # por outra causa cujo tail inclua essas linhas NÃO pode virar falso reseed.
    d = causa.classificar(_obito(exit_code=1, stderr=stderr))
    assert d.acao != "escalar_reseed", d


# --------------------------------------------------------------------------
# NAVEGAÇÃO abortada (net::ERR_ABORTED) — assinatura CLARA, não "desconhecida"
# --------------------------------------------------------------------------
# Cauda de stderr REAL do incidente Stoa 27/07 10:19: enumerate.open_course fez
# page.goto("/carrega/<token>") e o Chromium abortou (net::ERR_ABORTED). exit 1,
# traceback Playwright. A autópsia gravada escalou como "causa desconhecida e
# nenhum LLM disponível" (fail-closed cego) — sendo que a assinatura é CLARA:
# navegação abortada = bug do adaptador, não sessão morta.
_STDERR_STOA_ERR_ABORTED = (
    '  File ".../motor/stoa/enumerate.py", line 274, in open_course\n'
    '    return await _content_at(page, urljoin(base_url, f"/carrega/{card.token}"))\n'
    '  File ".../motor/stoa/enumerate.py", line 254, in _content_at\n'
    '    await page.goto(url, wait_until="domcontentloaded")\n'
    "playwright._impl._errors.Error: Page.goto: net::ERR_ABORTED at "
    "https://educacao.stoa.com.br/carrega/QVlRHZlbopUYxoFWXpmRpFmVwNXVxw2\n"
    "Call log:\n  - navigating to \"https://educacao.stoa.com.br/carrega/QVlR\", "
    'waiting until "domcontentloaded"\n'
)


def test_err_aborted_e_bug_de_navegacao_nao_desconhecida():
    # DENTES (incidente Stoa 27/07): net::ERR_ABORTED tem que ser classificado
    # DETERMINISTICAMENTE como bug de navegação — nunca cair no cego "causa
    # desconhecida e nenhum LLM disponível" (o que o daemon fez ao vivo).
    d = causa.classificar(_obito(exit_code=1, stderr=_STDERR_STOA_ERR_ABORTED))
    assert d.acao == "escalar_humano", d
    assert d.fonte == "deterministico", d          # decidido SEM LLM (a prova)
    assert "navegação" in d.motivo.lower(), d       # causa NOMEADA
    assert "ERR_ABORTED" in d.motivo, d
    assert "desconhecida" not in d.motivo.lower(), d
    # Texto NEUTRO quanto à sessão: o abort do /carrega não passou pela sonda de
    # sessão, então não se manda verificar a sessão — mas também NÃO se afirma
    # categoricamente que ela está viva (o motivo antigo afirmava, e era falso nos
    # aborts DENTRO da sonda/reseed — ver os testes abaixo).
    assert "sonda/reseed" not in d.motivo, d
    assert "NÃO é sessão morta" not in d.motivo, d


def test_err_aborted_dispensa_o_llm():
    # Mesmo com o LLM DESLIGADO (llm=None, como no daemon vivo), a classificação
    # sai determinística — o seam nunca é tocado. Se este teste falhar, a autópsia
    # voltou a depender do LLM para uma assinatura que é clara.
    chamado = []
    def llm(_):                                     # sentinela: NÃO pode ser chamado
        chamado.append(1)
        return '{"acao": "escalar_humano"}'
    d = causa.classificar(_obito(exit_code=1, stderr=_STDERR_STOA_ERR_ABORTED), llm=llm)
    assert d.fonte == "deterministico", d
    assert chamado == [], "o LLM foi consultado para uma assinatura determinística"


# Caudas de stderr REAIS (copiadas de ~/.athena-local/autopsias — só o texto; os
# arquivos vivos NÃO são lidos pelo teste). O abort acontece DENTRO da sonda/reseed de
# sessão: a autópsia não pode dizer "NÃO é sessão morta" — a sessão é a 1ª suspeita.
# 19/08 22:09 stoa-principal (exit 1): ensure_session -> do_reseed -> reseed_session
# abortou em connect.stoa.com.br/login/.
_STDERR_STOA_RESEED_ERR_ABORTED = (
    'Traceback (most recent call last):\n'
    '  File "<frozen runpy>", line 198, in _run_module_as_main\n'
    '  File "<frozen runpy>", line 88, in _run_code\n'
    '  File "/Users/guilhermerodrigues/teste/aula-2b/.claude/worktrees/adaptador-stoa/motor/stoa/__main__.py", line 4, in <module>\n'
    '    main()\n'
    '  File "/Users/guilhermerodrigues/teste/aula-2b/.claude/worktrees/adaptador-stoa/motor/stoa/cli.py", line 237, in main\n'
    '    rc = asyncio.run(_amain(argv))\n'
    '         ^^^^^^^^^^^^^^^^^^^^^^^^^\n'
    '  File "/Users/guilhermerodrigues/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/asyncio/runners.py", line 190, in run\n'
    '    return runner.run(main)\n'
    '           ^^^^^^^^^^^^^^^^\n'
    '  File "/Users/guilhermerodrigues/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/asyncio/runners.py", line 118, in run\n'
    '    return self._loop.run_until_complete(task)\n'
    '           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n'
    '  File "/Users/guilhermerodrigues/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/asyncio/base_events.py", line 654, in run_until_complete\n'
    '    return future.result()\n'
    '           ^^^^^^^^^^^^^^^\n'
    '  File "/Users/guilhermerodrigues/teste/aula-2b/.claude/worktrees/adaptador-stoa/motor/stoa/cli.py", line 174, in _amain\n'
    '    await ensure_session(\n'
    '  File "/Users/guilhermerodrigues/teste/aula-2b/.claude/worktrees/adaptador-stoa/motor/stoa/session.py", line 403, in ensure_session\n'
    '    if not await do_reseed():\n'
    '           ^^^^^^^^^^^^^^^^^\n'
    '  File "/Users/guilhermerodrigues/teste/aula-2b/.claude/worktrees/adaptador-stoa/motor/stoa/session.py", line 292, in reseed_session\n'
    '    await page.goto(login_url or home_url, wait_until="domcontentloaded")\n'
    '  File "/Users/guilhermerodrigues/teste/aula/.venv/lib/python3.11/site-packages/playwright/async_api/_generated.py", line 9764, in goto\n'
    '    await self._impl_obj.goto(\n'
    '  File "/Users/guilhermerodrigues/teste/aula/.venv/lib/python3.11/site-packages/playwright/_impl/_page.py", line 560, in goto\n'
    '    return await self._main_frame.goto(**locals_to_params(locals()))\n'
    '           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n'
    '  File "/Users/guilhermerodrigues/teste/aula/.venv/lib/python3.11/site-packages/playwright/_impl/_frame.py", line 156, in goto\n'
    '    await self._channel.send(\n'
    '  File "/Users/guilhermerodrigues/teste/aula/.venv/lib/python3.11/site-packages/playwright/_impl/_connection.py", line 69, in send\n'
    '    return await self._connection.wrap_api_call(\n'
    '           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n'
    '  File "/Users/guilhermerodrigues/teste/aula/.venv/lib/python3.11/site-packages/playwright/_impl/_connection.py", line 563, in wrap_api_call\n'
    '    raise rewrite_error(error, f"{parsed_st[\'apiName\']}: {error}") from None\n'
    'playwright._impl._errors.Error: Page.goto: net::ERR_ABORTED; maybe frame was detached?\n'
    'Call log:\n'
    '  - navigating to "https://connect.stoa.com.br/login/", waiting until "domcontentloaded"'
)

# 13/08 00:40 kajabi-principal (exit 1): ensure_session -> check -> probe_session
# abortou em /library. A autópsia de 18/08 10:54 tem a cauda BYTE-IDÊNTICA (conferido).
_STDERR_KAJABI_PROBE_ERR_ABORTED = (
    'Traceback (most recent call last):\n'
    '  File "<frozen runpy>", line 198, in _run_module_as_main\n'
    '  File "<frozen runpy>", line 88, in _run_code\n'
    '  File "/Users/guilhermerodrigues/teste/aula/motor/kajabi/__main__.py", line 4, in <module>\n'
    '    main()\n'
    '  File "/Users/guilhermerodrigues/teste/aula/motor/kajabi/cli.py", line 196, in main\n'
    '    rc = asyncio.run(_amain(argv))\n'
    '         ^^^^^^^^^^^^^^^^^^^^^^^^^\n'
    '  File "/Users/guilhermerodrigues/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/asyncio/runners.py", line 190, in run\n'
    '    return runner.run(main)\n'
    '           ^^^^^^^^^^^^^^^^\n'
    '  File "/Users/guilhermerodrigues/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/asyncio/runners.py", line 118, in run\n'
    '    return self._loop.run_until_complete(task)\n'
    '           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n'
    '  File "/Users/guilhermerodrigues/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/asyncio/base_events.py", line 654, in run_until_complete\n'
    '    return future.result()\n'
    '           ^^^^^^^^^^^^^^^\n'
    '  File "/Users/guilhermerodrigues/teste/aula/motor/kajabi/cli.py", line 147, in _amain\n'
    '    await ensure_session(\n'
    '  File "/Users/guilhermerodrigues/teste/aula/motor/kajabi/session.py", line 178, in ensure_session\n'
    '    if await check():\n'
    '       ^^^^^^^^^^^^^\n'
    '  File "/Users/guilhermerodrigues/teste/aula/motor/kajabi/session.py", line 102, in probe_session\n'
    '    await page.goto(library_url(home_url), wait_until="domcontentloaded")\n'
    '  File "/Users/guilhermerodrigues/teste/aula/.venv/lib/python3.11/site-packages/playwright/async_api/_generated.py", line 9764, in goto\n'
    '    await self._impl_obj.goto(\n'
    '  File "/Users/guilhermerodrigues/teste/aula/.venv/lib/python3.11/site-packages/playwright/_impl/_page.py", line 560, in goto\n'
    '    return await self._main_frame.goto(**locals_to_params(locals()))\n'
    '           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n'
    '  File "/Users/guilhermerodrigues/teste/aula/.venv/lib/python3.11/site-packages/playwright/_impl/_frame.py", line 156, in goto\n'
    '    await self._channel.send(\n'
    '  File "/Users/guilhermerodrigues/teste/aula/.venv/lib/python3.11/site-packages/playwright/_impl/_connection.py", line 69, in send\n'
    '    return await self._connection.wrap_api_call(\n'
    '           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n'
    '  File "/Users/guilhermerodrigues/teste/aula/.venv/lib/python3.11/site-packages/playwright/_impl/_connection.py", line 563, in wrap_api_call\n'
    '    raise rewrite_error(error, f"{parsed_st[\'apiName\']}: {error}") from None\n'
    'playwright._impl._errors.Error: Page.goto: net::ERR_ABORTED; maybe frame was detached?\n'
    'Call log:\n'
    '  - navigating to "https://nepq-training.mykajabi.com/library", waiting until "domcontentloaded"'
)

# 22/07 19:30 hotmart-principal: RUÍDO do asyncio ("Future exception was never
# retrieved") — navegações em SEGUNDO PLANO canceladas quando a página fechou. Não é a
# exceção que encerrou o processo (ninguém a aguardou): aparece até num run que termina
# LIMPO. Trecho real da cauda (3 linhas httpx benignas + os 2 blocos de ruído).
_RUIDO_ASYNCIO_ERR_ABORTED = (
    '2026-07-22 19:28:36,177 INFO httpx: HTTP Request: GET https://api.notion.com/v1/databases/d39ec1652ad64a999c3b4db7641abba3 "HTTP/1.1 200 OK"\n'
    '2026-07-22 19:28:36,516 INFO httpx: HTTP Request: POST https://api.notion.com/v1/data_sources/49c13155-b7bb-406e-a35e-6be85bac081a/query "HTTP/1.1 200 OK"\n'
    '2026-07-22 19:28:37,073 INFO httpx: HTTP Request: PATCH https://api.notion.com/v1/blocks/3a5978a1-669c-819a-81ec-f0f1b71a851f/children "HTTP/1.1 200 OK"\n'
    '2026-07-22 19:29:08,441 ERROR asyncio: Future exception was never retrieved\n'
    'future: <Future finished exception=Error(\'net::ERR_ABORTED; maybe frame was detached?\\nCall log:\\n  - navigating to "https://hotmart.com/pt-BR/club/ciro-gestor/products/5431484/content/o4Eg8Jwd7z", waiting until "domcontentloaded"\\n\')>\n'
    'playwright._impl._errors.Error: net::ERR_ABORTED; maybe frame was detached?\n'
    'Call log:\n'
    '  - navigating to "https://hotmart.com/pt-BR/club/ciro-gestor/products/5431484/content/o4Eg8Jwd7z", waiting until "domcontentloaded"\n'
    '\n'
    '2026-07-22 19:29:08,441 ERROR asyncio: Future exception was never retrieved\n'
    'future: <Future finished exception=Error(\'net::ERR_ABORTED; maybe frame was detached?\\nCall log:\\n  - navigating to "https://hotmart.com/pt-BR/club/ciro-gestor/products/5431484/content/RON9Nmwd7P", waiting until "domcontentloaded"\\n\')>\n'
    'playwright._impl._errors.Error: net::ERR_ABORTED; maybe frame was detached?\n'
    'Call log:\n'
    '  - navigating to "https://hotmart.com/pt-BR/club/ciro-gestor/products/5431484/content/RON9Nmwd7P", waiting until "domcontentloaded"'
)

# 25/07 07:59 stoa-principal (exit 1): o Mac DORMIU no meio do page.goto. Trecho real
# (a partir do quadro _content_at). Escalou ao vivo como "causa desconhecida".
_STDERR_STOA_IO_SUSPENDED = (
    '  File "/Users/guilhermerodrigues/teste/aula-2b/.claude/worktrees/adaptador-stoa/motor/stoa/enumerate.py", line 254, in _content_at\n'
    '    await page.goto(url, wait_until="domcontentloaded")\n'
    '  File "/Users/guilhermerodrigues/teste/aula/.venv/lib/python3.11/site-packages/playwright/async_api/_generated.py", line 9764, in goto\n'
    '    await self._impl_obj.goto(\n'
    '  File "/Users/guilhermerodrigues/teste/aula/.venv/lib/python3.11/site-packages/playwright/_impl/_page.py", line 560, in goto\n'
    '    return await self._main_frame.goto(**locals_to_params(locals()))\n'
    '           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n'
    '  File "/Users/guilhermerodrigues/teste/aula/.venv/lib/python3.11/site-packages/playwright/_impl/_frame.py", line 156, in goto\n'
    '    await self._channel.send(\n'
    '  File "/Users/guilhermerodrigues/teste/aula/.venv/lib/python3.11/site-packages/playwright/_impl/_connection.py", line 69, in send\n'
    '    return await self._connection.wrap_api_call(\n'
    '           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n'
    '  File "/Users/guilhermerodrigues/teste/aula/.venv/lib/python3.11/site-packages/playwright/_impl/_connection.py", line 563, in wrap_api_call\n'
    '    raise rewrite_error(error, f"{parsed_st[\'apiName\']}: {error}") from None\n'
    'playwright._impl._errors.Error: Page.goto: net::ERR_NETWORK_IO_SUSPENDED at https://educacao.stoa.com.br/carrega/QVlRHZlbopUYxoFWXpmRpFmVwNXVxw2UWFjSyNmRkFGZF9GeZFjW0ImVNpnWHh3UVJzZ4ZFWOdnYGlVP\n'
    'Call log:\n'
    '  - navigating to "https://educacao.stoa.com.br/carrega/QVlRHZlbopUYxoFWXpmRpFmVwNXVxw2UWFjSyNmRkFGZF9GeZFjW0ImVNpnWHh3UVJzZ4ZFWOdnYGlVP", waiting until "domcontentloaded"'
)

# 30/07 06:44 memberkit-mastertalkers (exit 1): a rede do SO MUDOU durante a SONDA de
# sessão (probe_session). Trecho real (a partir do quadro _amain).
_STDERR_MEMBERKIT_NETWORK_CHANGED = (
    '  File "/Users/guilhermerodrigues/teste/aula/motor/memberkit/cli.py", line 147, in _amain\n'
    '    await ensure_session(\n'
    '  File "/Users/guilhermerodrigues/teste/aula/motor/memberkit/session.py", line 155, in ensure_session\n'
    '    if await check():\n'
    '       ^^^^^^^^^^^^^\n'
    '  File "/Users/guilhermerodrigues/teste/aula/motor/memberkit/session.py", line 80, in probe_session\n'
    '    await page.goto(home_url, wait_until="domcontentloaded")\n'
    '  File "/Users/guilhermerodrigues/teste/aula/.venv/lib/python3.11/site-packages/playwright/async_api/_generated.py", line 9764, in goto\n'
    '    await self._impl_obj.goto(\n'
    '  File "/Users/guilhermerodrigues/teste/aula/.venv/lib/python3.11/site-packages/playwright/_impl/_page.py", line 560, in goto\n'
    '    return await self._main_frame.goto(**locals_to_params(locals()))\n'
    '           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n'
    '  File "/Users/guilhermerodrigues/teste/aula/.venv/lib/python3.11/site-packages/playwright/_impl/_frame.py", line 156, in goto\n'
    '    await self._channel.send(\n'
    '  File "/Users/guilhermerodrigues/teste/aula/.venv/lib/python3.11/site-packages/playwright/_impl/_connection.py", line 69, in send\n'
    '    return await self._connection.wrap_api_call(\n'
    '           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n'
    '  File "/Users/guilhermerodrigues/teste/aula/.venv/lib/python3.11/site-packages/playwright/_impl/_connection.py", line 563, in wrap_api_call\n'
    '    raise rewrite_error(error, f"{parsed_st[\'apiName\']}: {error}") from None\n'
    'playwright._impl._errors.Error: Page.goto: net::ERR_NETWORK_CHANGED at https://master-talkers.memberkit.com.br/\n'
    'Call log:\n'
    '  - navigating to "https://master-talkers.memberkit.com.br/", waiting until "domcontentloaded"'
)


@pytest.mark.parametrize("stderr,funcao", [
    (_STDERR_STOA_RESEED_ERR_ABORTED, "reseed_session"),      # Stoa 19/08
    (_STDERR_KAJABI_PROBE_ERR_ABORTED, "probe_session"),      # Kajabi 13/08 (== 18/08)
])
def test_err_aborted_dentro_da_sonda_de_sessao_manda_verificar_a_sessao(stderr, funcao):
    # DENTES (casos reais): o abort aconteceu DENTRO de ensure_session. O motivo
    # antigo afirmava "NÃO é sessão morta" — FALSO aqui: a sessão é a 1ª suspeita.
    # A AÇÃO segue escalar_humano (NUNCA reseed: sem prova de sessão morta, latchar o
    # curso seria o falso "Sessão expirou" permanente).
    d = causa.classificar(_obito(exit_code=1, stderr=stderr))
    assert d.acao == "escalar_humano", d
    assert d.fonte == "deterministico", d
    assert "DURANTE a sonda/reseed de sessão" in d.motivo, d
    assert "verifique a sessão antes de mexer no adaptador" in d.motivo, d
    assert funcao in d.motivo, d                   # ONDE abortou (a função mais interna)
    assert "ERR_ABORTED" in d.motivo, d
    assert "NÃO é sessão morta" not in d.motivo, d


def test_ruido_asyncio_nao_herda_quadros_de_sessao_de_traceback_vizinho():
    # A busca da função de sessão sobe SÓ pelo traceback que TERMINA no ERR_ABORTED.
    # Um traceback VIZINHO que passou por ensure_session e morreu de OUTRA coisa não
    # pode "emprestar" seus quadros ao ruído do asyncio (que não tem quadro nenhum).
    vizinho = ('Traceback (most recent call last):\n'
               '  File "/x/motor/kajabi/session.py", line 178, in ensure_session\n'
               '    if await check():\n'
               "KeyError: 'library'\n")
    assert causa._funcao_de_sessao_no_abort(vizinho + _RUIDO_ASYNCIO_ERR_ABORTED) is None
    assert causa._funcao_de_sessao_no_abort(_STDERR_STOA_ERR_ABORTED) is None   # /carrega
    assert causa._funcao_de_sessao_no_abort(
        _STDERR_STOA_RESEED_ERR_ABORTED) == "reseed_session"


def test_exit0_com_ruido_asyncio_err_aborted_e_saida_limpa():
    # DENTES [BLOQUEANTE da revisão]: o bloco ERR_ABORTED rodava ANTES do `code == 0`.
    # Um run que sai LIMPO mas cujo asyncio despejou o ruído "Future exception was
    # never retrieved ... net::ERR_ABORTED" (cauda real 22/07) virava escalar_humano —
    # e o athena_local só reconhece saída limpa com exit 0 E aguardar_backoff: o run
    # limpo virava MORTE (alerta essencial + backoff + cooldown perdido).
    d = causa.classificar(_obito(exit_code=0, stderr=_RUIDO_ASYNCIO_ERR_ABORTED))
    assert d.acao == "aguardar_backoff", d
    assert d.fonte == "deterministico", d
    assert "saída limpa" in d.motivo, d


def test_exit_nao_zero_com_so_o_ruido_nao_ganha_o_nome_de_navegacao():
    # REESCRITO (item 4 da rodada 3). O teste antigo exigia que exit 1 + SÓ o ruído do
    # asyncio fosse nomeado "bug de navegação" — mas o ruído é uma exceção que NINGUÉM
    # aguardou ("Future exception was never retrieved"): por construção não foi ela que
    # derrubou o processo, e a cauda não tem traceback algum que termine no abort. Nomear
    # era afirmar uma causa sem prova (o teste exigia o bug). Sem exceção terminal
    # conhecida, a causa é honestamente DESCONHECIDA -> LLM / fail-closed.
    d = causa.classificar(_obito(exit_code=1, stderr=_RUIDO_ASYNCIO_ERR_ABORTED))
    assert d.acao == "escalar_humano", d                 # fail-closed segue chamando o dono
    assert d.fonte == "fail-closed", d
    assert "navegação" not in d.motivo and "ERR_ABORTED" not in d.motivo, d


# Um traceback que TERMINA em OUTRA exceção (o que de fato matou o processo). Estrutura
# real de um crash do motor (runpy -> cli.main -> asyncio.run -> _amain ...).
_TRACEBACK_KEYERROR_DURATION = (
    'Traceback (most recent call last):\n'
    '  File "<frozen runpy>", line 198, in _run_module_as_main\n'
    '  File "<frozen runpy>", line 88, in _run_code\n'
    '  File "/Users/guilhermerodrigues/teste/aula/motor/kajabi/__main__.py", line 4, in <module>\n'
    '    main()\n'
    '  File "/Users/guilhermerodrigues/teste/aula/motor/kajabi/cli.py", line 196, in main\n'
    '    rc = asyncio.run(_amain(argv))\n'
    '         ^^^^^^^^^^^^^^^^^^^^^^^^^\n'
    '  File "/Users/guilhermerodrigues/teste/aula/motor/kajabi/cli.py", line 166, in _amain\n'
    '    stats = await run_kajabi_course(\n'
    '  File "/Users/guilhermerodrigues/teste/aula/motor/kajabi/pipeline.py", line 212, in _duracao\n'
    '    return int(meta["duration"])\n'
    '               ~~~~^^^^^^^^^^^^\n'
    "KeyError: 'duration'\n"
)


@pytest.mark.parametrize("cauda", [
    _RUIDO_ASYNCIO_ERR_ABORTED + "\n" + _TRACEBACK_KEYERROR_DURATION,   # ruído, depois o crash
    _TRACEBACK_KEYERROR_DURATION + _RUIDO_ASYNCIO_ERR_ABORTED,          # crash, depois o ruído (GC)
    (_STDERR_STOA_ERR_ABORTED                                            # abort ENCADEADO: o
     + "\nDuring handling of the above exception, another exception occurred:\n\n"
     + _TRACEBACK_KEYERROR_DURATION),                                    # terminal é o KeyError
], ids=["ruido-antes", "ruido-depois", "encadeado"])
def test_err_aborted_que_nao_e_a_excecao_terminal_nao_vira_bug_de_navegacao(cauda):
    # DENTES (item 4): exit 1 com traceback terminando em KeyError: 'duration' + ruído
    # do asyncio com ERR_ABORTED na cauda. O bloco antigo casava ERR_ABORTED em QUALQUER
    # linha e mandava o dono caçar um "bug de navegação" que não matou nada. Aqui a causa
    # é desconhecida (sem LLM no daemon => fail-closed), e com LLM ligado o seam É
    # consultado (não há assinatura determinística que o dispense).
    d = causa.classificar(_obito(exit_code=1, stderr=cauda))
    assert d.fonte == "fail-closed", d
    assert "navegação" not in d.motivo and "ERR_ABORTED" not in d.motivo, d
    chamado = []
    d2 = causa.classificar(_obito(exit_code=1, stderr=cauda),
                           llm=lambda p: chamado.append(p) or '{"acao": "escalar_humano"}')
    assert chamado and d2.fonte == "llm", d2


def test_err_aborted_terminal_com_ruido_depois_segue_nomeado():
    # Contraprova: o abort É a exceção terminal (Stoa /carrega 27/07) e o ruído do asyncio
    # vem DEPOIS (GC no fechamento) — ruído não é traceback, então não rouba o posto.
    d = causa.classificar(_obito(exit_code=1,
                                 stderr=_STDERR_STOA_ERR_ABORTED + _RUIDO_ASYNCIO_ERR_ABORTED))
    assert d.acao == "escalar_humano" and d.fonte == "deterministico", d
    assert "bug de navegação (net::ERR_ABORTED)" in d.motivo, d


def test_traceback_terminal_acha_a_excecao_mesmo_sem_cabecalho():
    # A janela da cauda (40 linhas) corta o cabeçalho "Traceback (most recent call
    # last):" dos tracebacks longos do Playwright (as caudas reais de 27/07 e 25/07
    # começam no meio dos quadros): a âncora são os QUADROS.
    exc, quadros = causa._traceback_terminal(_STDERR_STOA_ERR_ABORTED)
    assert exc.startswith("playwright._impl._errors.Error: Page.goto: net::ERR_ABORTED")
    assert quadros == ["open_course", "_content_at"]
    exc, quadros = causa._traceback_terminal(_TRACEBACK_KEYERROR_DURATION)
    assert exc == "KeyError: 'duration'" and quadros[2] == "<module>"
    assert causa._traceback_terminal(_RUIDO_ASYNCIO_ERR_ABORTED) is None


@pytest.mark.parametrize("stderr", [
    _STDERR_STOA_ERR_ABORTED,                   # /carrega (27/07)
    _STDERR_STOA_RESEED_ERR_ABORTED,            # reseed_session (19/08) — navega pro /login/
    _STDERR_KAJABI_PROBE_ERR_ABORTED,           # probe_session (13/08 e 18/08)
])
@pytest.mark.parametrize("acao_confusa", ["escalar_reseed", "escalar_token"])
def test_err_aborted_nao_e_confundido_com_sessao_nem_token(stderr, acao_confusa):
    # DENTES (reescrito — o antigo passava SEM o bloco ERR_ABORTED, porque o fail-closed
    # já dava escalar_humano). Com o diagnóstico por LLM LIGADO (ATHENA_CAUSA_LLM=1), um
    # LLM que lê "navigating to .../login/" e propõe reseed (IRREDUTÍVEL: latch do curso)
    # ou token NÃO pode decidir: a assinatura determinística vence e o seam nem é tocado.
    chamado = []

    def llm_confuso(prompt):
        chamado.append(prompt)
        return '{"acao": "%s"}' % acao_confusa

    d = causa.classificar(_obito(exit_code=1, stderr=stderr), llm=llm_confuso)
    assert d.acao == "escalar_humano", d
    assert d.fonte == "deterministico", d
    assert chamado == [], "o LLM foi consultado para uma assinatura determinística"


@pytest.mark.parametrize("stderr", [
    "Page.goto: net::ERR_NAME_NOT_RESOLVED at https://x/y",   # DNS não resolveu
    "net::ERR_CONNECTION_REFUSED",                             # conexão recusada
    "net::ERR_INTERNET_DISCONNECTED at https://x",            # link caiu
    "net::ERR_TIMED_OUT",                                     # timeout de rede
    "Page.goto: net::ERR_NETWORK_IO_SUSPENDED at https://x",  # Mac dormiu
    "Page.goto: net::ERR_NETWORK_CHANGED at https://x/",      # rede do SO mudou
    _STDERR_STOA_IO_SUSPENDED,                                # real: Stoa 25/07
    _STDERR_MEMBERKIT_NETWORK_CHANGED,                        # real: Memberkit 30/07 (na sonda)
])
def test_neterror_de_conectividade_e_transitorio_nao_desconhecida(stderr):
    # Os net-errors de CONECTIVIDADE (host/DNS/link/rede do SO/Mac dormindo) são
    # transitórios de rede — aguardar_backoff, não o cego "desconhecida". Distinto do
    # ERR_ABORTED. ERR_NETWORK_IO_SUSPENDED escalava humano ao vivo (22/07 e 25/07).
    d = causa.classificar(_obito(exit_code=1, stderr=stderr))
    assert d.acao == "aguardar_backoff", d
    assert d.fonte == "deterministico", d


# 22/07 10:22 memberkit-mastertalkers: cauda REAL (trecho final, conferido byte a byte
# contra ~/.athena-local/autopsias/20260722T102201_422033-memberkit-mastertalkers.json;
# o teste NÃO lê o arquivo vivo) de um page.goto que estourou 30s — a assinatura mais
# comum das autópsias (31 ocorrências).
_STDERR_MEMBERKIT_TIMEOUT_GOTO = (
    '  File "/Users/guilhermerodrigues/teste/aula/motor/memberkit/enumerate.py", line 201, in _content_at\n'
    '    await page.goto(url, wait_until="domcontentloaded")\n'
    '  File "/Users/guilhermerodrigues/teste/aula/.venv/lib/python3.11/site-packages/playwright/async_api/_generated.py", line 9764, in goto\n'
    '    await self._impl_obj.goto(\n'
    '  File "/Users/guilhermerodrigues/teste/aula/.venv/lib/python3.11/site-packages/playwright/_impl/_page.py", line 560, in goto\n'
    '    return await self._main_frame.goto(**locals_to_params(locals()))\n'
    '           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n'
    '  File "/Users/guilhermerodrigues/teste/aula/.venv/lib/python3.11/site-packages/playwright/_impl/_frame.py", line 156, in goto\n'
    '    await self._channel.send(\n'
    '  File "/Users/guilhermerodrigues/teste/aula/.venv/lib/python3.11/site-packages/playwright/_impl/_connection.py", line 69, in send\n'
    '    return await self._connection.wrap_api_call(\n'
    '           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n'
    '  File "/Users/guilhermerodrigues/teste/aula/.venv/lib/python3.11/site-packages/playwright/_impl/_connection.py", line 563, in wrap_api_call\n'
    '    raise rewrite_error(error, f"{parsed_st[\'apiName\']}: {error}") from None\n'
    'playwright._impl._errors.TimeoutError: Page.goto: Timeout 30000ms exceeded.\n'
    'Call log:\n'
    '  - navigating to "https://master-talkers.memberkit.com.br/219124-capital-creator-autoritha", waiting until "domcontentloaded"\n'
)


@pytest.mark.parametrize("stderr", [
    _STDERR_MEMBERKIT_TIMEOUT_GOTO,                       # real: TimeoutError do page.goto
    "asyncio.TimeoutError: operation timed out",
    "MemoryError",
    "Out of memory: Killed process 123",
], ids=["timeout-goto-real", "asyncio-timeout", "memoryerror", "oom-kill"])
def test_exit0_com_timeout_ou_oom_na_cauda_e_saida_limpa(stderr):
    # DENTES (mesma classe do bloqueante 1 da rodada 2): OOM/timeout rodavam ANTES do
    # teste de exit 0, então um run que saiu LIMPO com um timeout de aula (tratado e
    # retentado pelo motor) na cauda virava `relancar` — e o athena_local, que só
    # reconhece saída limpa com exit 0 E aguardar_backoff, contava FALHA no disjuntor.
    d = causa.classificar(_obito(exit_code=0, stderr=stderr))
    assert d.acao == "aguardar_backoff", d
    assert d.fonte == "deterministico", d
    assert "saída limpa" in d.motivo, d


def test_timeout_real_numa_morte_segue_relancar():
    # Contraprova: numa MORTE (exit != 0) a mesma cauda real segue transitório.
    d = causa.classificar(_obito(exit_code=1, stderr=_STDERR_MEMBERKIT_TIMEOUT_GOTO))
    assert d.acao == "relancar" and "timeout" in d.motivo, d


def test_rede_caida_vence_o_ruido_err_aborted_na_mesma_cauda():
    # Quando a rede cai/muda, as navegações em voo morrem junto e o asyncio despeja o
    # ruído ERR_ABORTED na MESMA cauda. A causa-raiz é a rede (backoff), não um "bug
    # de navegação" que chamaria o dono à toa.
    d = causa.classificar(_obito(
        exit_code=1, stderr=_RUIDO_ASYNCIO_ERR_ABORTED + "\n" + _STDERR_STOA_IO_SUSPENDED))
    assert d.acao == "aguardar_backoff", d
    assert d.fonte == "deterministico", d


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


# --------------------------------------------------------------------------
# FIX 1 (observabilidade) — o ramo exit-4 ENRIQUECE o motivo com os erros REAIS
# do tracker (SQLite READ-ONLY), em vez do hard-code "causa sistêmica desconhecida".
# --------------------------------------------------------------------------
import os
import sqlite3


def _tracker_com_erros(dirpath, linhas):
    """Cria um tracker.db com o MESMO schema do motor (aula/motor/tracker.py) e
    as linhas dadas: (course_id, status, error, updated_at)."""
    db = os.path.join(str(dirpath), "tracker.db")
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE lessons(
        course_id TEXT, order_idx INTEGER, url TEXT, status TEXT,
        notion_page_id TEXT, error TEXT, updated_at REAL)""")
    for i, (cid, status, error, ts) in enumerate(linhas):
        con.execute("INSERT INTO lessons VALUES (?,?,?,?,?,?,?)",
                    (cid, i, f"http://aula/{i}", status, None, error, ts))
    con.commit()
    con.close()
    return db


def test_exit4_enriquece_motivo_com_erros_reais_do_tracker(tmp_path):
    # RED-first: hoje o exit-4 hard-coda "causa sistêmica desconhecida" SEM ler a
    # coluna `error` do tracker — a autópsia morre cega. Com o fix, o motivo carrega
    # os TOP-3 erros mais recentes DO CURSO (e só dele).
    _tracker_com_erros(tmp_path, [
        ("6278192", "audio_erro", "TimeoutError: chunk 3 do audio estourou 120s", 100.0),
        ("6278192", "audio_erro", "HTTP 429 rate limit", 200.0),
        ("6278192", "erro", "player nao encontrado na aula 7", 300.0),
        ("6278192", "erro", "erro ANTIGO fora do top-3", 50.0),
        ("999", "erro", "erro de OUTRO curso", 400.0),
    ])
    o = _obito(exit_code=4, curso="https://hotmart.com/pt/x/products/6278192/agent",
               stderr=_ABORT_CIRCUIT_BREAKER)
    d = causa.classificar(o, tracker_dir=str(tmp_path))
    assert d.acao == "escalar_humano", d           # a AÇÃO não muda (fail-closed honesto)
    assert d.fonte == "deterministico", d
    # DENTES: o motivo agora carrega os erros REAIS (top-3 por recência), do curso certo
    assert "player nao encontrado na aula 7" in d.motivo, d
    assert "HTTP 429 rate limit" in d.motivo, d
    assert "TimeoutError: chunk 3" in d.motivo, d
    assert "fora do top-3" not in d.motivo, d      # limite 3, por recência
    assert "OUTRO curso" not in d.motivo, d        # só o curso do óbito


def test_exit4_sem_tracker_cai_no_motivo_generico(tmp_path):
    # Fail-safe: sem tracker.db legível, o motivo generico de hoje se mantém (não
    # levanta, não inventa) — e NADA é criado em disco (leitura mode=ro).
    o = _obito(exit_code=4, curso="https://hotmart.com/pt/x/products/111/",
               stderr=_ABORT_CIRCUIT_BREAKER)
    d = causa.classificar(o, tracker_dir=str(tmp_path))
    assert d.acao == "escalar_humano", d
    assert "desconhecida" in d.motivo, d
    assert not os.path.exists(os.path.join(str(tmp_path), "tracker.db"))


def test_exit4_curso_sem_product_id_cai_no_motivo_generico(tmp_path):
    # URL sem /products/<id> (ex.: Stoa/Memberkit): não há como resolver o course_id
    # do tracker Hotmart — cai no genérico, sem levantar.
    _tracker_com_erros(tmp_path, [("111", "erro", "irrelevante", 1.0)])
    d = causa.classificar(_obito(exit_code=4, curso="https://minha.memberkit.com.br/9",
                                 stderr=_ABORT_CIRCUIT_BREAKER),
                          tracker_dir=str(tmp_path))
    assert d.acao == "escalar_humano", d
    assert "desconhecida" in d.motivo, d


def test_exit4_erros_do_tracker_nao_mudam_a_acao_nem_viram_reseed(tmp_path):
    # INVARIANTE: um erro do tracker que CITA sessão/login NÃO pode reclassificar o
    # exit-4 (o texto vai pro MOTIVO, observabilidade; a ação segue escalar_humano).
    _tracker_com_erros(tmp_path, [
        ("6278192", "erro", "redirecionado pro login ao abrir a aula", 100.0)])
    o = _obito(exit_code=4, curso="https://x.com/products/6278192/",
               stderr=_ABORT_CIRCUIT_BREAKER)
    d = causa.classificar(o, tracker_dir=str(tmp_path))
    assert d.acao == "escalar_humano", d           # NÃO virou reseed pelo texto do tracker


# --------------------------------------------------------------------------
# FIX 2 (exit-5) — sonda de sessão INCONCLUSIVA / erro de infra (contrato do
# motor: SystemExit(5) em SessionProbeInconclusiveError e PWError). NÃO é sessão
# morta (anti-ban: jamais reseed/relogin por exit 5) — transitório: relança sob
# backoff. O bench anti-flap de exit-5 em série vive no loop (athena_local).
# --------------------------------------------------------------------------
def test_exit5_sonda_inconclusiva_relanca_deterministico_sem_llm():
    # RED-first: hoje exit-5 sem stderr (tee desligado) cai em DESCONHECIDA ->
    # escalar_humano/LLM. Com o fix, o contrato de exit-code decide: relancar.
    chamadas = []
    def llm(p):
        chamadas.append(p)
        return "relancar"
    d = causa.classificar(_obito(exit_code=5, stderr=""), llm=llm)
    assert d.acao == "relancar", d
    assert d.fonte == "deterministico", d          # decidido pelo exit-code
    assert chamadas == []                          # sem gastar claude -p
    assert "sonda" in d.motivo or "infra" in d.motivo, d


def test_exit5_com_stderr_real_do_motor_relanca():
    # A mensagem REAL do motor no exit 5 (contém "timeout", sem âncora de morte).
    d = causa.classificar(_obito(
        exit_code=5,
        stderr="SONDA DE SESSÃO INCONCLUSIVA (transitório de rede/timeout): "
               "probe /v1/navigation timeout"))
    assert d.acao == "relancar", d
    assert d.acao != "escalar_reseed", d


def test_exit5_com_stderr_de_sessao_morta_ainda_escala_reseed():
    # INVARIANTE anti-ban (precedência): se o stderr DECLARA a sessão morta, a
    # causa-raiz é a sessão — mesmo com exit 5. O exit-code NÃO mascara o veredito.
    d = causa.classificar(_obito(exit_code=5, stderr="SESSÃO MORTA: refaça o login"))
    assert d.acao == "escalar_reseed", d


# --------------------------------------------------------------------------
# EXIT 6 — completude FAIL-CLOSED / enumerador ausente (contrato novo do motor,
# commit 2aa84b9: Alpaclass e Nutror, EXIT_ENUMERACAO_INCOMPLETA = 6). Antes caía no
# fail-closed "causa desconhecida" SÓ porque o LLM está desligado no daemon.
# --------------------------------------------------------------------------
# Saída REAL dos CLIs (os `print` de motor/alpaclass/cli.py e motor/nutror/cli.py do
# 2aa84b9, com o `{e}` = a mensagem da exceção de motor/<plat>/enumerate.py).
_EXIT6_ALPACLASS = (
    "2026-09-10 19:20:01,100 INFO motor.alpaclass.enumerate: alpaclass: 1 curso(s) "
    "acessível(is) em 'meus cursos'\n"
    "\n"
    "ENUMERADOR DE AULAS ALPACLASS NÃO IMPLEMENTADO: enumerador de aulas Alpaclass não "
    "implementado: o curso acessível 'curso-x' existe, mas o shape de "
    "/learner/courses/<slug> (módulos/aulas) nunca foi observado ao vivo e não há parser "
    "— NÃO reporto 0 aulas como sucesso. Próximo passo: recon do detalhe do curso numa "
    "sessão com curso comprado e ligar o parser como `fetch_lessons` default.\n"
    "Nenhuma aula foi capturada e NADA foi marcado como concluído. Falta o parser do "
    "detalhe do curso (GET /learner/courses/<slug>) — recon numa sessão com curso "
    "comprado. Não é a sessão nem a rede.\n"
)
_EXIT6_NUTROR = (
    "\n"
    "ENUMERAÇÃO NUTROR VAZIA (parse do DOM quebrou): nenhum link de curso (/v3/curso/...) "
    "no DOM de https://x.nutror.com/ após autenticar — a SPA não hidratou os links ou o "
    "layout mudou. Confirme no ao-vivo (DevTools > Network logado) se a lista vem via "
    "JSON e ligue o seam `list_courses_via_api`. NÃO reportando sucesso com catálogo "
    "vazio.\n"
    "A sessão está viva mas nenhum curso/aula saiu do DOM — provável mudança de layout "
    "ou lista via JSON. Confirme no ao-vivo (DevTools > Network logado) e ligue o "
    "caminho por API antes de retomar.\n"
)


@pytest.mark.parametrize("stderr,ultima", [
    (_EXIT6_ALPACLASS, "Nenhuma aula foi capturada e NADA foi marcado como concluído. "
                       "Falta o parser do detalhe do curso (GET /learner/courses/<slug>) "
                       "— recon numa sessão com curso comprado. Não é a sessão nem a rede."),
    (_EXIT6_NUTROR, "A sessão está viva mas nenhum curso/aula saiu do DOM — provável "
                    "mudança de layout ou lista via JSON. Confirme no ao-vivo (DevTools > "
                    "Network logado) e ligue o caminho por API antes de retomar."),
], ids=["alpaclass", "nutror"])
def test_exit6_completude_fail_closed_e_causa_nomeada_sem_llm(stderr, ultima):
    # DENTES: sem o mapeamento, exit 6 não bate em assinatura nenhuma (nem sessão —
    # "A sessão está viva"/"Não é a sessão" não DECLARAM morte —, nem token, nem
    # timeout) e cai no LLM/fail-closed "causa desconhecida".
    chamado = []

    def llm(prompt):                                # sentinela: NÃO pode ser chamado
        chamado.append(prompt)
        return '{"acao": "relancar"}'

    d = causa.classificar(_obito(exit_code=6, stderr=stderr), llm=llm)
    assert d.acao == "escalar_humano", d
    assert d.fonte == "deterministico", d
    assert chamado == [], "o LLM foi consultado para o contrato de exit 6"
    assert d.motivo == "enumeração/completude fail-closed do adaptador: " + ultima, d
    assert "desconhecida" not in d.motivo, d
    assert d.acao != "escalar_reseed" and d.acao != "relancar", d


def test_exit6_sem_stderr_ainda_e_nomeado():
    # tee desligado / cauda vazia: o contrato de exit-code sozinho nomeia a causa.
    d = causa.classificar(_obito(exit_code=6, stderr=""))
    assert d.acao == "escalar_humano" and d.fonte == "deterministico", d
    assert d.motivo == "enumeração/completude fail-closed do adaptador: (stderr vazio)", d


# --------------------------------------------------------------------------
# MORTE SÓ-DE-LOCK (item 1 da rodada 3): captura disparada por uma encarnação ANTERIOR
# do loop — o exit code se perdeu com o Popen dela (exit_code None, detectado pelo PID
# morto do lock). A autópsia agora lê o .err da conta; a causa distingue:
#   - cauda que termina no RESUMO FINAL do motor  -> saída limpa ÓRFÃ (sem falha)
#   - lock anterior ao boot + cauda inconclusiva  -> reinício/desligamento do Mac (sem falha)
#   - cauda conclusiva                             -> a assinatura dela, como sempre
#   - nada disso (pós-boot, cauda muda)            -> desconhecida, fail-closed (como antes)
# --------------------------------------------------------------------------
def _orfa(stderr="", antes_do_boot=False):
    return Obito(conta="stoa-principal", curso="https://educacao.stoa.com.br/",
                 exit_code=None, stderr_tail=stderr, flaps_na_janela=1, ts="",
                 lock_mtime=1000.0, boot_ts=2000.0 if antes_do_boot else 500.0,
                 lock_antes_do_boot=antes_do_boot)


# Caudas REAIS de runs LIMPOS (~/.athena-local/motor-logs, 10/09 — só o texto; o teste
# não lê os arquivos vivos): o resumo final é a última linha que o CLI imprime antes de
# sair 0.
_CAUDA_LIMPA_STOA = (
    "2026-09-10 17:27:55,656 INFO motor.stoa.pipeline: stoa curso stoa:educacao:13594: ok=0 audio=0 falhou=0 de 0\n"
    "2026-09-10 17:27:55,656 INFO motor.stoa.pipeline: stoa curso stoa:educacao:12742: ok=0 audio=0 falhou=0 de 0\n"
    "2026-09-10 17:28:32,585 INFO motor.stoa.pipeline: stoa curso stoa:educacao:30709: ok=0 audio=4 falhou=0 de 4\n"
    "Stoa[audio]: cursos=21 total=4 ok=0 benigno=4 falhou=0\n"
)
_CAUDA_LIMPA_HOTMART = (
    "2026-09-10 17:22:42,987 INFO motor.hotmart.session: sessão viva (state)\n"
    "2026-09-10 17:22:43,933 INFO motor.hotmart.session: sessão persistida em .hotmart-session.json (151 cookies)\n"
    "2026-09-10 17:22:43,933 INFO motor.cli: sessão pronta (origem=state)\n"
    "2026-09-10 17:22:46,298 INFO motor.hotmart.enumerate: nome do curso capturado de page.title(): 'Treinamentos do Dailtinho' (bruto='Treinamentos do Dailtinho | Hotmart Club')\n"
    "Stats: total=0 ok=0 audio=0 falhou=0\n"
)


@pytest.mark.parametrize("cauda,resumo", [
    (_CAUDA_LIMPA_STOA, "Stoa[audio]: cursos=21 total=4 ok=0 benigno=4 falhou=0"),
    (_CAUDA_LIMPA_HOTMART, "Stats: total=0 ok=0 audio=0 falhou=0"),
    # o que pode vir DEPOIS do resumo num sucesso: 'Skills:' do Hotmart, aviso de
    # teardown, e o ruído do asyncio (não é traceback).
    (_CAUDA_LIMPA_HOTMART + "Skills: 3/4 geradas\n"
     "2026-09-10 17:22:47,001 WARNING motor.cli: falha ao fechar o contexto: x\n"
     + _RUIDO_ASYNCIO_ERR_ABORTED, "Stats: total=0 ok=0 audio=0 falhou=0"),
    # timeout de aula (tratado e retentado) ANTES do resumo: segue limpa (gate `limpa`)
    (_STDERR_MEMBERKIT_TIMEOUT_GOTO + "Memberkit: modulos=1 total=26 ok=25 sem_audio=0 falhou=1\n",
     "Memberkit: modulos=1 total=26 ok=25 sem_audio=0 falhou=1"),
], ids=["stoa-real", "hotmart-real", "hotmart-skills-teardown-ruido", "timeout-antes-do-resumo"])
def test_orfa_que_termina_no_resumo_final_e_saida_limpa_sem_falha(cauda, resumo):
    # DENTES: sem a regra, exit None + cauda limpa não bate em assinatura nenhuma e cai
    # no fail-closed "causa desconhecida" (alerta ESSENCIAL "MORREU" + backoff) — o ruído
    # das 9 autópsias detectado_por=pid reais (todas pós-bounce do loop pelo vigia).
    d = causa.classificar(_orfa(cauda))
    assert d.acao == "aguardar_backoff", d
    assert d.fonte == "deterministico", d
    assert d.sem_falha is True, d
    assert "saída limpa de captura órfã" in d.motivo and resumo in d.motivo, d


def test_resumo_com_traceback_depois_nao_e_saida_limpa():
    # O resumo seguido de um traceback = o processo morreu DEPOIS (teardown): não se
    # afirma saída limpa pela prosa.
    d = causa.classificar(_orfa(_CAUDA_LIMPA_HOTMART + _TRACEBACK_KEYERROR_DURATION))
    assert d.sem_falha is False and d.fonte == "fail-closed", d


@pytest.mark.parametrize("code", [1, 4, -9])
def test_exit_code_real_vence_o_resumo_da_cauda(code):
    # A prosa NUNCA sobrepõe um exit code REAL: o resumo só decide quando o exit é
    # desconhecido (morte só-de-lock).
    d = causa.classificar(_obito(exit_code=code, stderr=_CAUDA_LIMPA_STOA))
    assert d.sem_falha is False, d
    assert "órfã" not in d.motivo, d


@pytest.mark.parametrize("cauda", [
    "",                                                     # tee vazio/ausente
    "2026-09-10 03:10:01,000 INFO motor.stoa.pipeline: baixando aula 7/21\n",  # no meio do run
    # o shutdown mata o Chrome antes do Python: TargetClosedError (8 autópsias reais)
    'Traceback (most recent call last):\n'
    '  File "/Users/guilhermerodrigues/teste/aula/motor/stoa/enumerate.py", line 254, in _content_at\n'
    '    html = await page.content()\n'
    'playwright._impl._errors.TargetClosedError: Page.content: Target page, context or '
    'browser has been closed\n',
], ids=["vazia", "meio-do-run", "target-closed"])
def test_lock_antes_do_boot_sem_cauda_conclusiva_e_reinicio_do_mac(cauda):
    # DENTES (item 1b): a captura nasceu ANTES do boot atual e a cauda não aponta outra
    # causa -> o reinício/desligamento do Mac a matou: relancar SEM falha (sem alerta
    # essencial, sem backoff). Sem a regra: fail-closed "causa desconhecida".
    d = causa.classificar(_orfa(cauda, antes_do_boot=True))
    assert d.acao == "relancar", d
    assert d.fonte == "deterministico", d
    assert d.sem_falha is True, d
    assert d.motivo.startswith("reinício/desligamento do Mac"), d


@pytest.mark.parametrize("cauda,acao", [
    ("SESSÃO MORTA — LOGIN MANUAL NECESSÁRIO\n", "escalar_reseed"),   # a cauda DIZ a causa
    (_STDERR_MEMBERKIT_TIMEOUT_GOTO, "relancar"),
    (_STDERR_STOA_IO_SUSPENDED, "aguardar_backoff"),                 # o Mac dormiu antes
], ids=["sessao-morta", "timeout", "io-suspended"])
def test_lock_antes_do_boot_com_cauda_conclusiva_classifica_pela_cauda(cauda, acao):
    # "sem stderr conclusivo" é condição da regra do reinício: quando a cauda nomeia a
    # causa, ela vence — e aí é falha de verdade (conta no disjuntor como sempre).
    d = causa.classificar(_orfa(cauda, antes_do_boot=True))
    assert d.acao == acao and d.sem_falha is False, d
    assert "reinício" not in d.motivo, d


def test_lock_depois_do_boot_sem_cauda_segue_fail_closed():
    # Contraprova: a regra do reinício é CONDICIONADA ao boot. Uma órfã pós-boot que
    # morreu sem cauda nenhuma segue desconhecida (fail-closed, chama o dono).
    d = causa.classificar(_orfa("", antes_do_boot=False))
    assert d.acao == "escalar_humano" and d.fonte == "fail-closed", d
    assert d.sem_falha is False, d
