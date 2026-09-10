"""CAUSA — a causa-raiz de um Obito, classificada numa `Decisao(acao)` do conjunto
FECHADO de ações. É o "brainstorm autônomo" do vigia: dado como um filho morreu, decide
o QUE fazer — sem nunca propor login automático (inviolável anti-ban).

FLUXO (2 camadas, determinístico-primeiro):
  1. ASSINATURAS DETERMINÍSTICAS sobre (exit_code, stderr_tail):
       - stderr DECLARA sessão morta          -> escalar_reseed  (humano refaz login HEADED)
       - 401/403/"invalid api key"/auth-error -> escalar_token   (credencial de API ruim;
                                                                  NÃO o nome de um provedor
                                                                  num log de sucesso 200)
       - exit 2/3 (Session{Lost,Dead}/NavDead) -> escalar_reseed  (veredito do motor)
       - exit 4 (CircuitBreaker/excesso falhas) -> escalar_humano  (causa DESCONHECIDA)
       - exit 5 (sonda inconclusiva/infra)    -> relancar        (transitório; bench no loop)
       - exit 6 (completude fail-closed)      -> escalar_humano  (causa NOMEADA: enumerador
                                                                  do adaptador; última linha)
       - SIGKILL/OOM/timeout                  -> relancar        (transitório de recurso/SO)
       - net::ERR_<conectividade>, exit != 0  -> aguardar_backoff (rede caiu/mudou, Mac dormiu)
       - net::ERR_ABORTED, exit != 0          -> escalar_humano  (causa NOMEADA: sonda/reseed
                                                                  de sessão OU navegação)
       - transitório reconhecido / exit 0     -> aguardar_backoff (rede/5xx/saída limpa)
  2. DESCONHECIDA (não bate em nenhuma assinatura): consulta o SEAM `llm`.
       - produção: `claude -p` headless com a autópsia (ver `seam_claude_p`), cuja saída
         é VALIDADA contra o conjunto fechado.
       - llm ausente (None) / exceção / resposta FORA do conjunto -> escalar_humano
         (fail-closed: na dúvida, chama o dono, nunca chuta uma ação perigosa).

INVIOLÁVEL: `relogar` (login automático) NÃO existe no conjunto fechado e é RECUSADO se
o LLM ousar propô-lo (vira escalar_humano). A reautenticação é SEMPRE `escalar_reseed`:
um humano refaz o login HEADED no Mac; a Athena NUNCA loga sozinha.

PRECEDÊNCIA das assinaturas: sessão-assertiva > token > exit-code(2/3/4) > relancar >
backoff. A sessão-ASSERTIVA vem primeiro de propósito — se um filho morreu por SIGKILL
mas o stderr DECLARA sessão morta, a causa-raiz é a sessão (relançar sem reseed só
reproduziria a morte). A palavra "sessão" isolada NÃO é sinal: o motor a loga em operação
normal ("sessão viva/persistida") e o abort por excesso de falhas a cita como HIPÓTESE —
casá-la solta era o falso "Sessão expirou" que benchava o curso de vez (bug corrigido).
"""
import os
import re
import sqlite3
from dataclasses import dataclass


# Conjunto FECHADO de ações. `relogar`/`login` NÃO estão aqui — de propósito.
ACOES = {
    "escalar_reseed",       # sessão morta: humano refaz login HEADED (NUNCA automático)
    "escalar_token",        # credencial de API ruim: humano troca a chave
    "relancar",             # transitório de recurso/SO: o loop re-dispara (com backoff)
    "aguardar_backoff",     # transitório de rede/servidor: espera e o loop reavalia
    "escalar_humano",       # desconhecida/fail-closed: chama o dono
}

# O LLM só pode devolver uma destas (subconjunto do fechado). `relancar` incluso.
_ACOES_LLM = set(ACOES)

# Tokens de login AUTOMÁTICO que o LLM jamais pode nos fazer executar.
_PROIBIDOS = ("relogar", "relogin", "auto-login", "autologin", "auto login",
              "login automatico", "login automático", "fazer login sozinho")


@dataclass(frozen=True)
class Decisao:
    """A ação escolhida + de onde veio (auditoria). `fonte`: 'deterministico' | 'llm' |
    'fail-closed'."""
    acao: str
    motivo: str = ""
    fonte: str = ""


# --------------------------------------------------------------------------
# assinaturas determinísticas
# --------------------------------------------------------------------------
# Sessão morta / precisa relogar (HEADED, pelo humano). Palavras-âncora do domínio.
#
# Regra de ouro desta regex: ela casa só frases que DECLARAM a sessão morta —
# nunca a palavra "sessão"/"session" isolada. O motor loga "sessão viva",
# "sessão persistida", "sessão pronta" em operação NORMAL, e o abort por excesso
# de falhas LISTA "a sessão" como uma HIPÓTESE entre várias ("pode ser o
# anti-bot, a sessão, a rede..."). Casar a palavra solta transformava esses dois
# casos benignos em falso "Sessão expirou" — que ainda benchava o curso de vez
# (escalar_reseed é IRREDUTÍVEL, abre o disjuntor permanentemente). Por isso toda
# âncora PT exige um verbo/estado de morte junto (expir|mort|morr|perdida|
# inv[aá]lida|caiu), e as âncoras de login são FRASES ("refaça/faça o login",
# "redirecionado pro login"), nunca a palavra "login" nua.
_RE_SESSAO = re.compile(
    r"(session\s*expired|session\s*lost|session\s*(is\s+)?dead|session\s+died|"
    r"sessionlosterror|sessiondeaderror|navigationdeaderror|not\s+logged\s+in|"
    r"please\s+log\s*in|log\s*in\s+again|login\s+again|re-?login|"
    r"sess[aã]o(\s+\w+)?\s+(expir|mort|morr|perdida|inv[aá]lida|caiu)|"
    r"(re)?fa[çc]a\s+o\s+login|redirecionad\w*\s+pro\s+login|reautentic)", re.I)

# CONTRATO DE EXIT-CODE dos CLIs do motor (uniforme em TODAS as plataformas —
# motor/cli.py e motor/<plataforma>/cli.py fazem `raise SystemExit(N)`). O daemon
# spawna `python -m <modulo>` DIRETO (sem shell), então `proc.returncode` chega
# fiel ao Obito. É o sinal MAIS FORTE de causa-raiz que temos — mais confiável do
# que garimpar prosa —, porque o motor CODIFICA a semântica no código de saída:
#   2 -> SessionLostError                        (sessão morreu no meio do run)
#   3 -> SessionDeadError / NavigationDeadError   (morta/ilegível no startup)
#   4 -> CircuitBreakerError (EXCESSO DE FALHAS): causa DESCONHECIDA por
#        construção (anti-bot? rede? detector nosso ruim?). A mensagem apenas
#        LISTA a sessão como palpite — NÃO a declara morta. Tratar como reseed
#        aqui era o falso "Sessão expirou" que benchava o curso de vez.
#   5 -> SessionProbeInconclusiveError / PWError (infra): a sonda de sessão deu
#        timeout/erro transitório SEM sinal forte de logout, ou a infraestrutura
#        (DNS/conexão/URL) falhou ANTES de tocar a sessão. O motor sai FORA de
#        {2,3} DE PROPÓSITO: NÃO é veredito de sessão morta (anti-ban: jamais
#        reseed/relogin por exit 5) — transitório: relança sob o backoff. O
#        bench anti-flap de exit-5 EM SÉRIE (URL malformada torna a sonda
#        inconclusiva para sempre) vive no loop (athena_local), não aqui.
#   6 -> EXIT_ENUMERACAO_INCOMPLETA (Alpaclass e Nutror; motor 2aa84b9): COMPLETUDE
#        FAIL-CLOSED — a sessão autenticou, mas o adaptador não enumerou aula
#        nenhuma (enumerador de aulas não implementado, parse do DOM quebrado, conta
#        sem curso comprado). O motor sai 6 JUSTAMENTE para nunca reportar 'total=0'
#        com exit 0 (no daemon: saída limpa + cooldown de 6h, calado, pra sempre) nem
#        o 5 da sonda (relancar/bench). A mensagem do motor diz "Não é a sessão nem a
#        rede": NÃO é reseed nem transitório — o dev olha o adaptador. Causa NOMEADA,
#        decidida sem LLM (antes caía no fail-closed "desconhecida").
_EXIT_SESSAO_MORTA = frozenset({2, 3})
_EXIT_CIRCUIT_BREAKER = 4
_EXIT_SONDA_INCONCLUSIVA = 5
_EXIT_ENUMERACAO_INCOMPLETA = 6
_LINHA_TRUNCA = 300                  # a última linha do stderr vai pro alerta/JSON

# Credencial de API ruim (401/403/api key inválida). É o que escala TROCA DE TOKEN.
#
# REGRA DE OURO (irmã da _RE_SESSAO): casa só frases que DECLARAM uma FALHA de
# credencial — nunca o NOME de um provedor nem a palavra "api key" nuas. O motor
# loga o httpx de operação NORMAL, e uma transcrição/resumo BEM-SUCEDIDO emite
# `POST https://api.groq.com/... "HTTP/1.1 200 OK"`. A regex antiga tinha `groq`
# como alternativa NUA, então casava esse log de SUCESSO 200 e classificava um
# abort de causa desconhecida (exit 4) como "troque o token" -> irredutível ->
# a captura latchava de vez com AS CHAVES VÁLIDAS (incidente Stoa 21/07, 1h28
# parada; Groq e Anthropic testadas ao vivo = 200). O nome do provedor num log
# não é sinal de credencial ruim; só um 401/403/"invalid api key"/AuthenticationError
# é. Removidas as âncoras nuas `groq`/`x-api-key`/`api_key`; exigido um qualificador
# de FALHA junto de "api key". `\b40[13]\b` nu também caiu (um id/contagem 401/403
# em log benigno dava falso) — fica só 401/403 com contexto http/status, além de
# unauthorized/forbidden, que não aparecem em log de raspagem normal.
_RE_TOKEN = re.compile(
    r"(authenticationerror|permissiondeniederror|unauthorized|forbidden|"
    r"http\s*40[13]\b|status\s*40[13]\b|"
    r"invalid\s+api[_\s-]?key|incorrect\s+api[_\s-]?key|"
    r"api[_\s-]?key\s+(invalid|incorrect|missing|expired|revoked|not\s+found)|"
    r"(invalid|incorrect|missing|expired|revoked)\s+api[_\s-]?key|"
    r"insufficient[_\s]permission)", re.I)

# OOM / falta de recurso -> relançar (o SO matou; provável transitório de memória).
_RE_OOM = re.compile(
    r"(out\s+of\s+memory|oom[-_\s]?kill|memoryerror|cannot\s+allocate|"
    r"killed\s+process|no\s+space\s+left)", re.I)
# Timeout -> relançar (a captura estourou o relógio; retomar é idempotente no motor).
_RE_TIMEOUT = re.compile(r"(time(d)?\s*out|timeouterror|timeout)", re.I)

# Transitório de rede/servidor -> aguardar backoff (espera e o loop reavalia).
_RE_TRANSITORIO = re.compile(
    r"(connection\s+reset|connectionreseterror|connection\s+refused|"
    r"temporarily\s+unavailable|try\s+again|\b50[0234]\b|service\s+unavailable|"
    r"bad\s+gateway|gateway\s+timeout|econnreset|network\s+is\s+unreachable)", re.I)

# net-errors de CONECTIVIDADE do Chromium -> aguardar backoff (transitório de rede):
# host não resolveu, conexão recusada/resetada, internet caiu, timeout de rede, a REDE
# DO SO MUDOU (ERR_NETWORK_CHANGED: Wi-Fi trocou/VPN) ou o SO SUSPENDEU o I/O de rede
# (ERR_NETWORK_IO_SUSPENDED: o Mac DORMIU no meio do page.goto — autópsias Stoa 22/07
# 10:54 e 25/07 07:59, ambas escaladas como "causa desconhecida"). O host/DNS/link/SO
# falhou ANTES de a navegação lógica acontecer. NÃO confundir com net::ERR_ABORTED
# (_RE_NAV_ABORTED). Checado ANTES do ERR_ABORTED de propósito: quando a rede cai/muda,
# as navegações em voo morrem junto e o asyncio despeja ruído "net::ERR_ABORTED; maybe
# frame was detached?" na mesma cauda — a causa-raiz é a rede, não o adaptador.
_RE_NET_CONECTIVIDADE = re.compile(
    r"net::err_(connection_[a-z_]+|name_not_resolved|internet_disconnected|"
    r"timed_out|address_unreachable|network_changed|network_io_suspended|"
    r"socket_not_connected)", re.I)

# NAVEGAÇÃO abortada pelo Chromium (net::ERR_ABORTED no page.goto). Assinatura CLARA,
# decidida SEM LLM (incidente Stoa 27/07: enumerate.open_course abortou em
# /carrega/<token>, exit 1, e a autópsia caiu no cego "causa desconhecida e nenhum LLM
# disponível"). NÃO é credencial de API nem rede caída (o host resolveu e conectou) —
# mas também NÃO se pode afirmar que a sessão está VIVA: o abort acontece DENTRO da
# sonda/reseed de sessão (ensure_session -> probe_session/reseed_session; casos reais
# Stoa 19/08 22:09 em reseed_session e Kajabi 13/08 e 18/08 em probe_session), e aí a
# sessão é a primeira suspeita. Por isso o MOTIVO depende de ONDE abortou (ver
# _funcao_de_sessao_no_abort). A AÇÃO é escalar_humano nos dois casos — NUNCA
# escalar_reseed (irredutível: latcharia o curso sem prova de sessão morta).
# SÓ vale para exit != 0: o asyncio imprime "Future exception was never retrieved ...
# net::ERR_ABORTED; maybe frame was detached?" até num run que termina LIMPO (ruído de
# navegação em segundo plano cancelada no fechamento da página — visto na cauda real
# de 22/07 19:30). Esse ruído não pode quebrar a saída limpa (athena_local).
_RE_NAV_ABORTED = re.compile(r"net::err_aborted", re.I)

# Funções dos motores (motor/<plataforma>/session.py) que SONDAM ou RESSEMEIAM a sessão.
# `do_reseed` é o apelido local do reseed_session em ensure_session (Stoa).
_FUNCS_SESSAO = frozenset({"ensure_session", "probe_session", "reseed_session",
                           "do_reseed"})
# Um quadro de traceback Python: `  File "<path>", line N, in <funcao>`.
_RE_FRAME = re.compile(r'^\s*File "[^"]*", line \d+, in (\w+)')


def _funcao_de_sessao_no_abort(err):
    """Nome da função de SESSÃO (_FUNCS_SESSAO) por onde passa o traceback que TERMINA
    numa linha com net::ERR_ABORTED — ou None. Sobe a partir da linha do erro pelos
    quadros do MESMO traceback (linhas `File ...`, código e `^^^` indentados) e para no
    primeiro limite do bloco (cabeçalho "Traceback", linha de log, outra exceção). Assim
    um ERR_ABORTED de RUÍDO do asyncio ("future: <Future ... ERR_ABORTED ...>", sem
    quadros) nunca herda os quadros de um traceback vizinho. Devolve a função mais
    INTERNA encontrada (probe_session/reseed_session antes de ensure_session)."""
    linhas = err.splitlines()
    for i, linha in enumerate(linhas):
        if not _RE_NAV_ABORTED.search(linha):
            continue
        for anterior in reversed(linhas[:i]):
            m = _RE_FRAME.match(anterior)
            if m:
                if m.group(1) in _FUNCS_SESSAO:
                    return m.group(1)
                continue
            if not anterior.startswith((" ", "\t")):
                break                    # fim do bloco deste traceback
    return None

# exit codes de SIGKILL: -9 (Popen) e 137 (128+9, via shell).
_EXIT_SIGKILL = {-9, 137}


def _ultima_linha(err):
    """Última linha NÃO-vazia do stderr (a mensagem final do motor), truncada para o
    alerta/JSON. '(stderr vazio)' quando não há nada — o motivo nunca fica oco."""
    for linha in reversed((err or "").splitlines()):
        if linha.strip():
            return linha.strip()[:_LINHA_TRUNCA]
    return "(stderr vazio)"


# --------------------------------------------------------------------------
# Enriquecimento do exit-4 com os erros REAIS do tracker (observabilidade).
#
# O exit 4 é, POR CONSTRUÇÃO, "excesso de falhas de causa desconhecida" — mas o
# tracker do motor GRAVA o erro de cada aula que falhou (coluna `error` de
# `lessons`). Ler os top-N mais recentes do curso e colá-los no MOTIVO transforma
# a autópsia cega ("causa sistêmica desconhecida") em "causa: <erro real>" — sem
# mudar a AÇÃO (segue escalar_humano, fail-closed honesto: reclassificar pela
# prosa do tracker reabriria a porta dos falsos reseed/token).
# --------------------------------------------------------------------------
# Onde mora o tracker.db: o motor_dir do daemon (mesmo default do
# athena_local.main). Injetável nos testes via `classificar(tracker_dir=...)`.
_MOTOR_DIR_PADRAO = "/Users/guilhermerodrigues/teste/aula"
_ERROS_TRACKER_LIMITE = 3            # top-3 erros recentes no motivo
_ERRO_TRUNCA = 160                   # truncagem por erro (o motivo vai p/ alerta/JSON)


def _course_id_de_url(curso_url):
    """product-id Hotmart do trecho `/products/<id>` da URL — replicado (de
    propósito) de `adaptadores.captura._course_id_de_url`, para a causa não
    acoplar à cadeia de imports do executor (mesmo padrão do `vigia._pid_vivo`).
    None quando a URL não tem o trecho (Stoa/Memberkit: sem enriquecimento)."""
    if not curso_url:
        return None
    achados = re.findall(r"/products/([^/?#]+)", str(curso_url))
    return achados[-1] if achados else None


def _erros_recentes_tracker(curso_url, tracker_dir=None,
                            limite=_ERROS_TRACKER_LIMITE):
    """Top-`limite` erros mais RECENTES do curso, lidos de `{tracker_dir}/tracker.db`
    em SQLite READ-ONLY (uri `mode=ro` + busy_timeout — padrão de
    `captura._pendencias_tracker`: NÃO muta e NÃO trava o motor vivo; o WAL permite
    leitura concorrente). `tracker_dir=None` resolve pelo env ATHENA_MOTOR_DIR (o
    mesmo do daemon). Devolve [] em QUALQUER erro (db ausente, course_id não
    resolve, SQL) — observabilidade JAMAIS derruba a classificação."""
    course_id = _course_id_de_url(curso_url)
    if not course_id:
        return []
    if tracker_dir is None:
        tracker_dir = os.getenv("ATHENA_MOTOR_DIR", _MOTOR_DIR_PADRAO)
    db_path = os.path.join(str(tracker_dir), "tracker.db")
    if not os.path.exists(db_path):
        return []
    con = None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        con.execute("PRAGMA busy_timeout=5000")
        linhas = con.execute(
            "SELECT error FROM lessons WHERE course_id=? AND error IS NOT NULL "
            "AND error != '' ORDER BY updated_at DESC LIMIT ?",
            (course_id, int(limite))).fetchall()
    except sqlite3.Error:
        return []
    finally:
        if con is not None:
            con.close()
    return [str(e)[:_ERRO_TRUNCA] for (e,) in linhas if e]


def _deterministico(obito, tracker_dir=None):
    """Devolve (acao, motivo) se bater numa assinatura conhecida; None se DESCONHECIDA."""
    err = obito.stderr_tail or ""
    code = obito.exit_code

    # 1) SESSÃO ASSERTIVA (precedência máxima): stderr que DECLARA a sessão morta.
    #    Vem antes de tudo — inclusive antes do exit-code — porque se o filho foi
    #    morto (SIGKILL) mas denunciou sessão expirada, a causa-raiz é a sessão
    #    (relançar sem reseed reproduziria a morte). Só frases de morte casam aqui;
    #    "sessão viva"/"pode ser a sessão" NÃO (ver _RE_SESSAO).
    if _RE_SESSAO.search(err):
        return "escalar_reseed", "assinatura de sessão morta no stderr"

    # 2) TOKEN de API (GROQ/401/403/api key): credencial ruim, troca de chave.
    #    Antes do exit-code do circuit-breaker: se as aulas falharam por chave de
    #    API inválida (o abort vira exit 4), a causa acionável é o TOKEN, não um
    #    "olhe o tracker" genérico.
    if _RE_TOKEN.search(err):
        return "escalar_token", "assinatura de credencial de API inválida no stderr"

    # 3) CONTRATO DE EXIT-CODE do motor (sinal FORTE, autoritativo):
    #    2/3 = sessão morta declarada pelo próprio motor -> reseed (substitui a
    #          antiga rede-de-segurança que garimpava a palavra "sessão"/"login" e
    #          disparava falso em log benigno / no abort por excesso de falhas).
    if code in _EXIT_SESSAO_MORTA:
        return "escalar_reseed", "exit code de sessão morta do motor (%s)" % code
    #    4 = CircuitBreakerError: EXCESSO DE FALHAS de causa DESCONHECIDA. NÃO é
    #        veredito de sessão — a mensagem só lista a sessão como hipótese. Vai
    #        pro humano olhar o tracker (fail-closed honesto), sem falso "expirou"
    #        e sem bench permanente do curso. Curto-circuita ANTES de qualquer
    #        heurística de prosa para a palavra "sessão" do abort nunca reativar o
    #        falso reseed.
    if code == _EXIT_CIRCUIT_BREAKER:
        #        OBSERVABILIDADE: o motivo carrega os erros REAIS do tracker (a
        #        coluna `error` das aulas que falharam) — "causa: <erro real>" em
        #        vez do cego "desconhecida". A AÇÃO não muda: reclassificar pela
        #        prosa do tracker reabriria os falsos reseed/token (as regexes de
        #        sessão/token operam sobre o STDERR do processo, não sobre o
        #        histórico do tracker).
        erros = _erros_recentes_tracker(getattr(obito, "curso", None), tracker_dir)
        if erros:
            return "escalar_humano", (
                "circuit-breaker por excesso de falhas (exit 4); erros recentes "
                "do tracker: " + " | ".join(erros) +
                " — humano decide; NÃO é sessão morta")
        return "escalar_humano", (
            "circuit-breaker por excesso de falhas (exit 4): causa sistêmica "
            "desconhecida — humano inspeciona o tracker; NÃO é sessão morta")

    # 4) EXIT 5 = sonda de sessão INCONCLUSIVA / erro de infra (contrato do motor):
    #    transitório — relança sob o backoff. NUNCA reseed (anti-ban: a sessão NÃO
    #    foi provada morta; as âncoras assertivas de sessão/token acima têm
    #    precedência caso o stderr declare outra coisa). Determinístico aqui poupa
    #    o LLM quando o tee de stderr está desligado (stderr vazio).
    if code == _EXIT_SONDA_INCONCLUSIVA:
        return "relancar", (
            "sonda de sessão inconclusiva / erro de infra (exit 5): transitório — "
            "NÃO é sessão morta (sem reseed)")

    # 4b) EXIT 6 = completude FAIL-CLOSED / enumerador ausente (Alpaclass, Nutror): o
    #     adaptador não enumerou aula nenhuma com a sessão VIVA. Humano/dev olha o
    #     adaptador — causa NOMEADA com a última linha do motor (a mensagem acionável),
    #     sem LLM. Depois de sessão/token (se o stderr DECLARA outra coisa, ela vence).
    if code == _EXIT_ENUMERACAO_INCOMPLETA:
        return "escalar_humano", (
            "enumeração/completude fail-closed do adaptador: %s" % _ultima_linha(err))

    # 5) SIGKILL / OOM / timeout -> relançar (transitório de recurso/SO; motor é idempotente).
    if code in _EXIT_SIGKILL:
        return "relancar", "exit code de SIGKILL (%s)" % code
    if _RE_OOM.search(err):
        return "relancar", "assinatura de OOM/falta de recurso no stderr"
    if _RE_TIMEOUT.search(err):
        return "relancar", "assinatura de timeout no stderr"

    # 6) net-errors de CONECTIVIDADE do Chromium (DNS/conexão/link/rede do SO mudou/Mac
    #    dormiu) numa MORTE (exit != 0): transitório de rede -> backoff. Antes do
    #    ERR_ABORTED (a rede caída arrasta navegações em voo; ver _RE_NET_CONECTIVIDADE).
    #    Com exit 0 cai no 8) — saída limpa, o motivo de sempre.
    if code != 0 and _RE_NET_CONECTIVIDADE.search(err):
        return "aguardar_backoff", (
            "net-error de conectividade do Chromium no stderr (rede caiu/mudou ou o "
            "Mac dormiu): transitório de rede")

    # 7) NAVEGAÇÃO abortada (net::ERR_ABORTED) numa MORTE (exit != 0 — com exit 0 é o
    #    ruído do asyncio num run limpo, e a saída limpa vence no 8). Determinístico
    #    aqui mata a autópsia cega "causa desconhecida e nenhum LLM disponível" (Stoa
    #    27/07). A AÇÃO é escalar_humano, com a causa NOMEADA: se o traceback do abort
    #    passa pela sonda/reseed de sessão, a SESSÃO é a 1ª suspeita (NÃO se afirma que
    #    está viva); senão, bug de navegação do adaptador. Vem DEPOIS de sessão/token/
    #    exit-code/OOM/conectividade (aquilo, se presente, é a causa-raiz).
    if code != 0 and _RE_NAV_ABORTED.search(err):
        fn_sessao = _funcao_de_sessao_no_abort(err)
        if fn_sessao:
            return "escalar_humano", (
                "navegação abortada DURANTE a sonda/reseed de sessão (net::ERR_ABORTED "
                "em %s) — verifique a sessão antes de mexer no adaptador" % fn_sessao)
        return "escalar_humano", (
            "bug de navegação (net::ERR_ABORTED): page.goto abortou ao abrir a página "
            "(rota/token inválido, redirect inesperado ou download no lugar da "
            "página) — dev verifica a navegação do adaptador")

    # 8) TRANSITÓRIO de rede/servidor, ou saída LIMPA (a completude é do loop/Notion).
    if code == 0:
        return "aguardar_backoff", "saída limpa (exit 0) — completude é do owner/Notion"
    if _RE_TRANSITORIO.search(err):
        return "aguardar_backoff", "assinatura de transitório de rede/servidor no stderr"

    return None                        # DESCONHECIDA -> seam do LLM


# --------------------------------------------------------------------------
# seam do LLM (produção: claude -p headless)
# --------------------------------------------------------------------------
def _prompt(obito) -> str:
    return (
        "Você é o diagnosticador de causa-raiz da Athena. Um subprocesso de captura "
        "MORREU. Classifique a causa e escolha UMA ação do conjunto FECHADO, respondendo "
        "APENAS com um JSON {\"acao\": \"<uma-das-ações>\"} e nada mais.\n\n"
        "Ações permitidas (e SÓ estas):\n"
        "  - relancar: transitório; basta re-disparar a captura.\n"
        "  - aguardar_backoff: transitório de rede/servidor; esperar e o loop reavalia.\n"
        "  - escalar_reseed: a SESSÃO morreu; um humano precisa refazer o login.\n"
        "  - escalar_token: uma credencial de API (ex.: GROQ) está inválida.\n"
        "  - escalar_humano: não dá para classificar com segurança.\n\n"
        "REGRA INVIOLÁVEL: NUNCA proponha login automático ('relogar'). Reautenticação é "
        "SEMPRE 'escalar_reseed' (humano refaz o login).\n\n"
        "Dados do óbito:\n"
        "  conta: %s\n  curso: %s\n  exit_code: %s\n  flaps_na_janela: %s\n"
        "  stderr (tail):\n%s\n" % (
            obito.conta, obito.curso, obito.exit_code, obito.flaps_na_janela,
            obito.stderr_tail or "(vazio)"))


def seam_claude_p(prompt: str) -> str:  # pragma: no cover — subprocesso REAL do claude
    """SEAM DE PRODUÇÃO: chama `claude -p` headless e devolve o stdout. Injetável nos
    testes (por isso classificar aceita `llm=`). O daemon liga `llm=causa.seam_claude_p`."""
    import subprocess
    r = subprocess.run(["claude", "-p", prompt], capture_output=True, text=True,
                       timeout=120)
    return r.stdout


def _parse_acao_llm(resp: str):
    """Extrai UMA ação válida da resposta do LLM. Recusa login automático e respostas
    ambíguas/fora-do-conjunto (-> None, que o chamador converte em escalar_humano)."""
    if not resp:
        return None
    baixo = resp.lower()

    # tenta JSON explícito {"acao": "..."} primeiro (o formato pedido no prompt).
    m = re.search(r'"acao"\s*:\s*"([a-z_]+)"', baixo)
    if m:
        cand = m.group(1)
        if cand in _ACOES_LLM:
            return cand
        return None                     # JSON com ação fora do conjunto: recusa

    # fallback: procura tokens de ação soltos no texto.
    achados = [a for a in _ACOES_LLM if a in baixo]
    # Se o LLM ousou propor login automático e NÃO deu uma ação segura clara: recusa.
    if any(p in baixo for p in _PROIBIDOS) and len(achados) != 1:
        return None
    if len(achados) == 1:
        return achados[0]
    return None                         # zero ou ambíguo -> fail-closed


def classificar(obito, llm=None, tracker_dir=None):
    """Classifica um `Obito` numa `Decisao`. `llm` é o seam do diagnosticador (callable
    prompt->texto); None => nenhum LLM disponível => fail-closed em escalar_humano quando
    a causa é desconhecida. Produção passa `llm=seam_claude_p`. `tracker_dir` aponta o
    diretório do tracker.db p/ enriquecer o motivo do exit-4 (None => env
    ATHENA_MOTOR_DIR, o default do daemon)."""
    det = _deterministico(obito, tracker_dir=tracker_dir)
    if det is not None:
        acao, motivo = det
        return Decisao(acao=acao, motivo=motivo, fonte="deterministico")

    # DESCONHECIDA -> seam do LLM.
    if llm is None:
        return Decisao(acao="escalar_humano",
                       motivo="causa desconhecida e nenhum LLM disponível",
                       fonte="fail-closed")
    try:
        resp = llm(_prompt(obito))
    except Exception as e:               # LLM indisponível/quebrado -> fail-closed
        return Decisao(acao="escalar_humano",
                       motivo="LLM falhou: %s" % e, fonte="fail-closed")

    acao = _parse_acao_llm(resp if isinstance(resp, str) else str(resp))
    if acao is None or acao not in ACOES:
        return Decisao(acao="escalar_humano",
                       motivo="resposta do LLM fora do conjunto fechado",
                       fonte="fail-closed")
    return Decisao(acao=acao, motivo="classificado pelo LLM", fonte="llm")
