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
