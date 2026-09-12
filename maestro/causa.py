"""CAUSA — a causa-raiz de um Obito, classificada numa `Decisao(acao)` do conjunto
FECHADO de ações. É o "brainstorm autônomo" do vigia: dado como um filho morreu, decide
o QUE fazer — sem nunca propor login automático (inviolável anti-ban).

FLUXO (2 camadas, determinístico-primeiro):
  1. ASSINATURAS DETERMINÍSTICAS sobre (exit_code, stderr_tail):
       - stderr DECLARA sessão morta          -> escalar_reseed  (humano refaz login HEADED)
       - falha de CHAVE DE API declarada    -> escalar_token   (AuthenticationError,
         (ver `_sinal_de_credencial`)                          "invalid api key", ou 401/403
                                                               COM contexto de api-key na
                                                               MESMA linha. NUNCA o nome de
                                                               um provedor num log 200, e
                                                               NUNCA "forbidden"/"unauthorized"
                                                               nuas — o 403 da CDN do yt-dlp
                                                               deu 23 falsos em 7 dias)
       - exit 2/3 (Session{Lost,Dead}/NavDead) -> escalar_reseed  (veredito do motor)
       - exit 4 (CircuitBreaker/excesso falhas) -> escalar_humano  (causa DESCONHECIDA)
       - exit 5 (sonda inconclusiva/infra)    -> relancar        (transitório; bench no loop)
       - exit 6 (completude fail-closed)      -> escalar_humano  (causa NOMEADA: enumerador
                                                                  do adaptador; última linha)
       - SIGKILL/OOM/timeout                  -> relancar        (transitório de recurso/SO)
       - net::ERR_<conectividade>, exit != 0  -> aguardar_backoff (rede caiu/mudou, Mac dormiu)
       - net::ERR_ABORTED como exceção        -> escalar_humano  (causa NOMEADA: sonda/reseed
         TERMINAL do traceback, exit != 0                         de sessão OU navegação)
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
A mesma regra vale para o TOKEN: "forbidden"/"unauthorized" nuas eram o falso "troque o
token" — 23 de 23 escaladas em 7 dias, todas pelo 403 da CDN do yt-dlp (ver
`_sinal_de_credencial`).
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
    'fail-closed'.

    `sem_falha`: o óbito NÃO é falha da captura — (a) captura ÓRFÃ (de encarnação
    anterior do loop, sem exit code observável) cuja cauda termina no RESUMO FINAL do
    motor = saída limpa; (b) captura disparada ANTES do boot atual e sem outra causa na
    cauda = o reinício/desligamento do Mac a matou. O loop NÃO conta falha no disjuntor,
    NÃO alerta e NÃO conta flap (athena_local._aplicar_decisao / vigia)."""
    acao: str
    motivo: str = ""
    fonte: str = ""
    sem_falha: bool = False


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

# NEGAÇÃO explícita de morte de sessão, removida da cauda ANTES de procurar a declaração.
# O motor Hotmart loga na RETENTATIVA da sonda: "sonda: /v1/navigation não respondeu em
# 30000ms (tentativa 1/2) — pode ser lentidão, não sessão morta; retentando" (real — 13
# autópsias têm a linha). A _RE_SESSAO casava "sessão mort" DENTRO dessa negação ->
# escalar_reseed (IRREDUTÍVEL: latch do curso + "Sessão expirou") num run que seguiu SÃO,
# até com exit 0. Agora que a morte só-de-lock lê o .err, a mesma linha chegaria às caudas
# das órfãs — daí o fix aqui. A morte REAL segue com os sinais dela ("sessão morta
# (state)", "SESSÃO MORTA — LOGIN MANUAL NECESSÁRIO", exit 2/3), que não são negados.
_RE_SESSAO_NEGADA = re.compile(
    r"\bn[aã]o\s+(?:[eé]\s+)?(?:a\s+)?sess[aã]o(?:\s+\w+)?\s+"
    r"(?:expir|mort|morr|perdida|inv[aá]lida|caiu)\w*|"
    r"\bsess[aã]o\s+n[aã]o\s+(?:\w+\s+)?(?:expir|mort|morr|perdida|inv[aá]lida|caiu)\w*",
    re.I)

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

# Credencial de API ruim (chave de API recusada). É o que escala TROCA DE TOKEN.
#
# REGRA DE OURO (irmã da _RE_SESSAO): casa só frases que DECLARAM uma FALHA DE
# CHAVE DE API — nunca o NOME de um provedor, nunca a palavra "api key" nua e
# NUNCA um 401/403/"unauthorized"/"forbidden" SOLTO.
#
# 1ª rodada do fix (incidente Stoa 21/07): a regex tinha `groq` como alternativa
# NUA e casava o httpx de SUCESSO `POST https://api.groq.com/... "HTTP/1.1 200 OK"`
# -> um abort de causa desconhecida (exit 4) virava "troque o token" e a captura
# latchava 1h28 COM AS CHAVES VÁLIDAS. Saíram as âncoras nuas de provedor.
#
# 2ª rodada (ESTE fix, medido em 12/09 sobre ~/.athena-local/autopsias): sobraram
# `unauthorized` e `forbidden` NUAS. Resultado: das 23 escaladas `escalar_token`
# dos últimos 7 dias, 23 (CEM POR CENTO) foram FALSAS — 16 Greenn + 7 Alpaclass,
# todas exit 4, todas casando SÓ a palavra "Forbidden" da linha do yt-dlp
#   `ERROR: unable to download video data: HTTP Error 403: Forbidden`
# que é a CDN do vídeo recusando o segmento (anti-bot/URL assinada vencida), não
# uma chave de API. A consequência é tripla e cara: (a) o alerta ESSENCIAL manda o
# dono trocar um token que está bom (alarme que mente = alarme que se ignora);
# (b) a causa REAL (excesso de falhas, exit 4, que pede olhar o tracker) fica
# escondida atrás do veredito errado; (c) o `escalar_token` conta falha no
# disjuntor com alerta, em vez do `escalar_humano` nomeado do exit 4.
# Varredura das 677 autópsias em disco: 95 linhas com "forbidden" — TODAS a mesma
# linha do yt-dlp; ZERO "unauthorized"; ZERO "authenticationerror"; ZERO
# "permissiondenied"; ZERO 401/403 com contexto http/status. Isto é: as âncoras
# nuas só produziram falso, e nenhum caso legítimo depende delas.
#
# O QUE CASA AGORA, em duas camadas:
#   (A) _RE_TOKEN_FORTE — a frase, sozinha, DECLARA a falha de credencial
#       (AuthenticationError, PermissionDeniedError, invalid_api_key,
#       "Incorrect API key provided", "API_KEY invalid", "insufficient permissions").
#   (B) proximidade POR LINHA — um 401/403/unauthorized/forbidden só vale quando a
#       MESMA linha traz vocabulário de CHAVE DE API ("api key"/"api_key"/
#       "x-api-key"/"apikey"/"credential"). Linha, não cauda inteira: "na mesma
#       cauda" juntaria um 403 de CDN com um "api_key" de outro log 40 linhas
#       adiante — que é exatamente o falso que estamos matando.
# O vocabulário de contexto EXCLUI de propósito `token`, `bearer` e `authorization`:
# a cauda real da Alpaclass tem
#   "a API learner recusou o Bearer do run em /lessons/... (401/USR_04) — relendo o
#    token do perfil/arquivo, sondando e renovando"
# que é o motor RENOVANDO a sessão com sucesso. Aceitar `token`/`bearer` como
# contexto transformaria esse log benigno no mesmo falso alarme de novo.
_RE_TOKEN_FORTE = re.compile(
    r"(authenticationerror|authentication[_\s]error|"
    r"permissiondeniederror|permission[_\s]denied(error)?|"
    r"invalid[_\s]api[_\s-]?key|"
    r"(invalid|incorrect|missing|expired|revoked|bad|wrong)\s+api[_\s-]?key|"
    r"api[_\s-]?key\s+(is\s+)?(invalid|incorrect|missing|expired|revoked|"
    r"not\s+found|was\s+not\s+provided)|"
    r"no\s+api[_\s-]?key\s+provided|"
    r"insufficient[_\s]permission)", re.I)

# (B) o 401/403 — só com contexto de CHAVE DE API na MESMA linha.
_RE_TOKEN_40X = re.compile(r"(\b40[13]\b|unauthorized|forbidden)", re.I)
_RE_TOKEN_CTX = re.compile(r"(api[_\s-]?key|x-api-key|apikey|credential)", re.I)


def _sinal_de_credencial(err):
    """True se a cauda DECLARA uma falha de CHAVE DE API (ver as duas camadas acima).

    Substitui o antigo `_RE_TOKEN.search(err)`: a palavra "forbidden"/"unauthorized"
    nua deixou de ser sinal. Mantida como função (e não como uma regex só) porque a
    camada (B) é uma regra de PROXIMIDADE POR LINHA, que regex de cauda inteira não
    expressa sem casar coisas a 40 linhas de distância."""
    if not err:
        return False
    if _RE_TOKEN_FORTE.search(err):
        return True
    for linha in err.splitlines():
        if _RE_TOKEN_40X.search(linha) and _RE_TOKEN_CTX.search(linha):
            return True
    return False


# ÂNCORA DE MASCARAMENTO (NÃO é o classificador). `maestro.rotulo` importa esta
# regex para MASCARAR texto que vem de FORA (título/URL de curso que a plataforma
# escolheu: ".../Unauthorized-Access-101", ".../forbidden-secrets") antes de ele
# entrar num alerta/log. Ali a regra é o OPOSTO da classificação: mascarar DEMAIS é
# barato (o rótulo sai com «…» e o dono ainda reconhece o curso), deixar passar é
# que é caro — o texto voltaria pela cauda de outro processo e casaria o
# classificador. Por isso ela CONTINUA larga (com `forbidden`/`unauthorized` nuas)
# mesmo depois de o classificador ter deixado de aceitá-las: defesa em profundidade,
# e o daemon VIVO ainda roda o classificador largo até este fix subir.
# QUEM DECIDE A CAUSA é `_sinal_de_credencial` (acima) — nunca esta regex.
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
# E SÓ quando o ERR_ABORTED é a exceção TERMINAL do traceback (ver _traceback_terminal):
# um processo que morreu de OUTRA exceção (ex.: KeyError: 'duration') com o ruído do
# asyncio na mesma cauda NÃO pode ganhar o texto "bug de navegação" — o ruído é, por
# definição, uma exceção que ninguém aguardou: não foi ela que derrubou o processo.
_RE_NAV_ABORTED = re.compile(r"net::err_aborted", re.I)

# Funções dos motores (motor/<plataforma>/session.py) que SONDAM ou RESSEMEIAM a sessão.
# `do_reseed` é o apelido local do reseed_session em ensure_session (Stoa).
_FUNCS_SESSAO = frozenset({"ensure_session", "probe_session", "reseed_session",
                           "do_reseed"})
# Um quadro de traceback Python: `  File "<path>", line N, in <funcao>` (`<module>`
# incluso — por isso `\S+`, não `\w+`).
_RE_FRAME = re.compile(r'^\s*File "[^"]*", line \d+, in (\S+)')


def _traceback_terminal(err):
    """(linha da exceção, [funções dos quadros, de fora p/ dentro]) do ÚLTIMO traceback
    Python da cauda — ou None se a cauda não tem traceback.

    Um traceback = uma sequência de QUADROS (`File "...", line N, in f`, com as linhas
    indentadas de código/`^^^` de cada um) encerrada pela 1ª linha NÃO-indentada: a
    exceção que ele levantou. A âncora são os quadros, não o cabeçalho "Traceback (most
    recent call last):", que a janela da cauda (40 linhas) costuma cortar. Exceções
    encadeadas ("During handling of the above exception...") produzem vários
    tracebacks: vale o ÚLTIMO — o que de fato derrubou o processo. O ruído do asyncio
    ("Future exception was never retrieved" + `future: <...>` + a exceção) NÃO tem
    quadros: não é traceback, logo nunca é a exceção terminal."""
    ultimo = None
    quadros = None                      # None = fora de um traceback
    for linha in (err or "").splitlines():
        m = _RE_FRAME.match(linha)
        if m:
            if quadros is None:
                quadros = []
            quadros.append(m.group(1))
            continue
        if quadros is None:
            continue
        if linha.startswith((" ", "\t")):
            continue                    # código / ^^^ / repr do quadro em curso
        if not linha.strip():
            quadros = None              # traceback cortado sem exceção: descarta
            continue
        ultimo = (linha, quadros)       # a exceção que ENCERROU este traceback
        quadros = None
    return ultimo


def _abort_terminal(err):
    """O traceback terminal SE a exceção dele é net::ERR_ABORTED; senão None."""
    tb = _traceback_terminal(err)
    if tb is not None and _RE_NAV_ABORTED.search(tb[0]):
        return tb
    return None


def _funcao_de_sessao_no_abort(err):
    """Nome da função de SESSÃO (_FUNCS_SESSAO) por onde passa o traceback TERMINAL da
    cauda, quando ele termina em net::ERR_ABORTED — ou None. Só os quadros DESSE
    traceback contam: um ERR_ABORTED de RUÍDO do asyncio (sem quadros) nunca herda os
    quadros de um traceback vizinho, e um traceback que passou pela sessão mas morreu de
    OUTRA exceção não é abort. Devolve a função mais INTERNA (probe_session/
    reseed_session antes de ensure_session)."""
    tb = _abort_terminal(err)
    if tb is None:
        return None
    for fn in reversed(tb[1]):
        if fn in _FUNCS_SESSAO:
            return fn
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


# RESUMO FINAL do motor: a linha que TODO CLI imprime SÓ no caminho de SUCESSO, logo antes
# de sair 0 — motor/cli.py (Hotmart): "Stats: total=N ok=N audio=N falhou=N";
# motor/<plat>/cli.py: "<Plataforma>[...]: <x>=N total=N ok=N <y>=N falhou=N" (Kajabi,
# Memberkit, Stoa[modo], Kiwify, Nutror, Alpaclass, Hubla, Greenn, Curseduca, Cademí).
# Os abortos controlados (exit 2/3/4/5/6) imprimem OUTRA mensagem e um crash imprime
# traceback — nenhum imprime o resumo. As linhas de log do pipeline ("...: ok=0 audio=0
# falhou=0 de 0") e o "Parando com ok=... falhou=N de M" do circuit-breaker NÃO casam
# (sem `total=`, e terminam em "de N").
_RE_RESUMO_FINAL = re.compile(
    r"^(?:Stats|\S.*?):\s(?:\S+=\d+\s+)*total=\d+\s+ok=\d+\s+\S+=\d+\s+falhou=\d+\s*$")


def _resumo_final(err):
    """A linha do RESUMO FINAL do motor quando a cauda termina nele — o run chegou ao
    `return 0` —, senão None. "Termina nele" = nenhum traceback DEPOIS do resumo: o que
    pode vir depois é só o fim do sucesso (as linhas 'Skills:' do Hotmart, avisos de
    fechamento do contexto, o ruído do asyncio), e nada disso é traceback. Usado SÓ
    quando o exit code é desconhecido (morte só-de-lock): um exit code REAL sempre vence
    a prosa."""
    linhas = (err or "").splitlines()
    idx = None
    for i, linha in enumerate(linhas):
        if _RE_RESUMO_FINAL.match(linha):
            idx = i
    if idx is None:
        return None
    if any(_RE_FRAME.match(l) or l.startswith("Traceback (most recent call last)")
           for l in linhas[idx + 1:]):
        return None
    return linhas[idx].strip()[:_LINHA_TRUNCA]


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


# --------------------------------------------------------------------------
# NOME DA PLATAFORMA do run — para a mensagem do circuit-breaker não mentir.
#
# O motor emite, em TODA plataforma, o mesmo texto do abort por excesso de falhas:
# "Algo está sistematicamente errado do lado da Hotmart" (motor/orchestrator.py —
# fora deste repositório). Nos runs de Greenn, Kiwify e Alpaclass isso é FALSO e
# manda o dono olhar o lugar errado. Enquanto o motor não for corrigido, a causa
# NOMEIA a plataforma REAL do run no motivo que vai ao alerta e ao JSON da
# autópsia — o texto que o dono lê deixa de apontar para a Hotmart.
#
# A fonte preferida é o rótulo do YAML (`plataforma=` — "greenn", "alpaclass"),
# que só o loop conhece; sem ele, o HOST da URL do curso (replicado de propósito,
# como `_course_id_de_url`, para a causa não acoplar à cadeia de imports do
# executor). "" quando nem isso dá — e aí a frase simplesmente não nomeia nada,
# nunca chuta "Hotmart".
_RE_HOST = re.compile(r"^[a-z][a-z0-9+.-]*://([^/?#]*)", re.I)


def _nome_plataforma(obito, plataforma=None):
    """Rótulo da plataforma do run ("greenn", "alpaclass", "sierramkt.greenn.club"...),
    ou "" se indeterminável. NUNCA levanta e NUNCA inventa um default."""
    if plataforma:
        return str(plataforma).strip()
    curso = getattr(obito, "curso", None)
    if not curso:
        return ""
    m = _RE_HOST.match(str(curso).strip())
    host = (m.group(1) if m else "").split("@")[-1].split(":")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    return host


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


def _deterministico(obito, tracker_dir=None, plataforma=None):
    """Devolve (acao, motivo) — ou (acao, motivo, sem_falha) nas duas regras de morte
    SEM FALHA da captura (ver `Decisao.sem_falha`) — se bater numa assinatura conhecida;
    None se DESCONHECIDA."""
    err = obito.stderr_tail or ""
    code = obito.exit_code
    # SAÍDA LIMPA: exit 0 REAL, ou — morte SÓ-DE-LOCK (exit code desconhecido: captura
    # ÓRFÃ de uma encarnação anterior do loop, cujo .err a autópsia leu) — cauda que
    # termina no RESUMO FINAL do motor. A órfã limpa recebe o MESMO tratamento do exit 0
    # nas assinaturas de morte abaixo (OOM/timeout/rede/abort não a transformam em morte).
    resumo = _resumo_final(err) if code is None else None
    orfa_limpa = resumo is not None
    limpa = code == 0 or orfa_limpa

    # 1) SESSÃO ASSERTIVA (precedência máxima): stderr que DECLARA a sessão morta.
    #    Vem antes de tudo — inclusive antes do exit-code — porque se o filho foi
    #    morto (SIGKILL) mas denunciou sessão expirada, a causa-raiz é a sessão
    #    (relançar sem reseed reproduziria a morte). Só frases de morte casam aqui;
    #    "sessão viva"/"pode ser a sessão" NÃO (ver _RE_SESSAO), nem a NEGAÇÃO "não
    #    sessão morta" do log de retentativa da sonda (ver _RE_SESSAO_NEGADA).
    if _RE_SESSAO.search(_RE_SESSAO_NEGADA.sub(" ", err)):
        return "escalar_reseed", "assinatura de sessão morta no stderr"

    # 2) TOKEN de API (chave recusada): credencial ruim, troca de chave.
    #    Antes do exit-code do circuit-breaker: se as aulas falharam por chave de
    #    API inválida (o abort vira exit 4), a causa acionável é o TOKEN, não um
    #    "olhe o tracker" genérico. É POR ESSA PRECEDÊNCIA que a exigência de
    #    contexto em `_sinal_de_credencial` é obrigatória: enquanto bastava a
    #    palavra "Forbidden" nua, todo exit 4 cuja cauda tivesse o 403 da CDN do
    #    yt-dlp era sequestrado aqui — 23 de 23 escaladas de token em 7 dias.
    if _sinal_de_credencial(err):
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
        #        PLATAFORMA NOMEADA: o abort do motor diz "do lado da Hotmart" em
        #        TODA plataforma. Aqui o motivo nomeia a do RUN (Greenn, Kiwify,
        #        Alpaclass...) — o dono para de ser mandado para a Hotmart.
        nome = _nome_plataforma(obito, plataforma)
        onde = (" na plataforma %s" % nome) if nome else ""
        if erros:
            return "escalar_humano", (
                "circuit-breaker por excesso de falhas (exit 4)" + onde +
                "; erros recentes do tracker: " + " | ".join(erros) +
                " — humano decide; NÃO é sessão morta")
        return "escalar_humano", (
            "circuit-breaker por excesso de falhas (exit 4)" + onde + ": causa "
            "sistêmica desconhecida — humano inspeciona o tracker; NÃO é sessão morta")

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
    #    OOM/timeout SÓ numa MORTE (exit != 0): exit 0 é saída LIMPA, e a cauda de um run
    #    limpo carrega timeouts/erros de aula já tratados e retentados pelo motor (ex.:
    #    "TimeoutError: Page.goto: Timeout 30000ms exceeded" de uma aula que falhou e o
    #    run seguiu). Sem o gate, o exit 0 virava `relancar` -> o athena_local (que só
    #    reconhece saída limpa com exit 0 E aguardar_backoff) contava FALHA no disjuntor
    #    e perdia o cooldown de concluído — mesma classe do ERR_ABORTED abaixo.
    if code in _EXIT_SIGKILL:
        return "relancar", "exit code de SIGKILL (%s)" % code
    if not limpa and _RE_OOM.search(err):
        return "relancar", "assinatura de OOM/falta de recurso no stderr"
    if not limpa and _RE_TIMEOUT.search(err):
        return "relancar", "assinatura de timeout no stderr"

    # 6) net-errors de CONECTIVIDADE do Chromium (DNS/conexão/link/rede do SO mudou/Mac
    #    dormiu) numa MORTE (exit != 0): transitório de rede -> backoff. Antes do
    #    ERR_ABORTED (a rede caída arrasta navegações em voo; ver _RE_NET_CONECTIVIDADE).
    #    Com exit 0 cai no 8) — saída limpa, o motivo de sempre.
    if not limpa and _RE_NET_CONECTIVIDADE.search(err):
        return "aguardar_backoff", (
            "net-error de conectividade do Chromium no stderr (rede caiu/mudou ou o "
            "Mac dormiu): transitório de rede")

    # 7) NAVEGAÇÃO abortada (net::ERR_ABORTED) numa MORTE (exit != 0 — com exit 0 é o
    #    ruído do asyncio num run limpo, e a saída limpa vence no 8) e SÓ quando ele é a
    #    exceção TERMINAL do traceback (morreu de outra exceção + ruído ERR_ABORTED na
    #    cauda => não é abort; segue adiante, sem o nome). Determinístico aqui mata a
    #    autópsia cega "causa desconhecida e nenhum LLM disponível" (Stoa 27/07). A AÇÃO
    #    é escalar_humano, com a causa NOMEADA: se o traceback do abort passa pela
    #    sonda/reseed de sessão, a SESSÃO é a 1ª suspeita (NÃO se afirma que está viva);
    #    senão, bug de navegação do adaptador. Vem DEPOIS de sessão/token/exit-code/OOM/
    #    conectividade (aquilo, se presente, é a causa-raiz).
    if not limpa and _abort_terminal(err) is not None:
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
    if orfa_limpa:
        # A órfã que terminou LIMPA (o run chegou ao resumo final; o loop que a
        # disparou morreu/foi reiniciado e levou o Popen — exit code não observável).
        # NÃO é morte: sem falha, sem alerta (era o falso "MORREU ... causa
        # desconhecida" das autópsias detectado_por=pid pós-bounce do loop).
        return "aguardar_backoff", (
            "saída limpa de captura órfã (disparada por encarnação anterior do loop; "
            "exit code não observável): a cauda do .err termina no resumo final do "
            "motor — %s" % resumo), True
    if _RE_TRANSITORIO.search(err):
        return "aguardar_backoff", "assinatura de transitório de rede/servidor no stderr"

    # 9) REINÍCIO/DESLIGAMENTO DO MAC: morte só-de-lock cujo lock (= o disparo) é
    #    ANTERIOR ao boot atual (kern.boottime) e cuja cauda não bateu em NENHUMA
    #    assinatura acima (sem stderr conclusivo). Nenhum processo atravessa um reboot:
    #    a captura morreu com o Mac, não por culpa dela. Relança SEM alerta essencial e
    #    SEM contar falha no disjuntor (uma autópsia por conta que capturava, em todo
    #    reboot, virava "causa desconhecida (fail-closed)" + backoff). Por último de
    #    propósito: se a cauda DIZ a causa (sessão morta, token, timeout...), ela vence.
    if code is None and getattr(obito, "lock_antes_do_boot", False):
        return "relancar", (
            "reinício/desligamento do Mac: a captura foi disparada antes do boot atual "
            "e a cauda não aponta outra causa — relanço sem alerta e sem contar falha"), True

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


def classificar(obito, llm=None, tracker_dir=None, plataforma=None):
    """Classifica um `Obito` numa `Decisao`. `llm` é o seam do diagnosticador (callable
    prompt->texto); None => nenhum LLM disponível => fail-closed em escalar_humano quando
    a causa é desconhecida. Produção passa `llm=seam_claude_p`. `tracker_dir` aponta o
    diretório do tracker.db p/ enriquecer o motivo do exit-4 (None => env
    ATHENA_MOTOR_DIR, o default do daemon). `plataforma` é o rótulo do YAML do curso
    ("greenn", "alpaclass"...): entra no motivo do exit-4 para a mensagem nomear a
    plataforma do RUN — o abort do motor diz "do lado da Hotmart" em todas elas. None
    => o host da URL do curso; indeterminável => a frase não nomeia ninguém."""
    det = _deterministico(obito, tracker_dir=tracker_dir, plataforma=plataforma)
    if det is not None:
        acao, motivo = det[0], det[1]
        return Decisao(acao=acao, motivo=motivo, fonte="deterministico",
                       sem_falha=len(det) > 2 and bool(det[2]))

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
