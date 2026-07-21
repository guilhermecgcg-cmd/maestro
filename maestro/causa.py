"""CAUSA — a causa-raiz de um Obito, classificada numa `Decisao(acao)` do conjunto
FECHADO de ações. É o "brainstorm autônomo" do vigia: dado como um filho morreu, decide
o QUE fazer — sem nunca propor login automático (inviolável anti-ban).

FLUXO (2 camadas, determinístico-primeiro):
  1. ASSINATURAS DETERMINÍSTICAS sobre (exit_code, stderr_tail):
       - sessão/login/expirou            -> escalar_reseed   (o humano refaz o login HEADED)
       - GROQ/401/403/api key            -> escalar_token    (credencial de API ruim)
       - SIGKILL/OOM/timeout             -> relancar         (transitório de recurso/SO)
       - transitório reconhecido / exit 0 -> aguardar_backoff (rede/5xx/saída limpa)
  2. DESCONHECIDA (não bate em nenhuma assinatura): consulta o SEAM `llm`.
       - produção: `claude -p` headless com a autópsia (ver `seam_claude_p`), cuja saída
         é VALIDADA contra o conjunto fechado.
       - llm ausente (None) / exceção / resposta FORA do conjunto -> escalar_humano
         (fail-closed: na dúvida, chama o dono, nunca chuta uma ação perigosa).

INVIOLÁVEL: `relogar` (login automático) NÃO existe no conjunto fechado e é RECUSADO se
o LLM ousar propô-lo (vira escalar_humano). A reautenticação é SEMPRE `escalar_reseed`:
um humano refaz o login HEADED no Mac; a Athena NUNCA loga sozinha.

PRECEDÊNCIA das assinaturas: reseed > token > relancar > backoff. A sessão vem primeiro
de propósito — se um filho morreu por SIGKILL mas o stderr denuncia sessão expirada, a
causa-raiz é a sessão (relançar sem reseed só reproduziria a morte).
"""
import re
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
_RE_SESSAO = re.compile(
    r"\b(session\s*expired|session\s*lost|sessionlosterror|not\s+logged\s+in|"
    r"please\s+log\s*in|log\s*in\s+again|login\s+again|re-?login|"
    r"sess[aã]o\s+expir|refa[çc]a\s+o\s+login|reautentic)", re.I)
# Rede de segurança (depois do token), para frasear sessão que a regex forte não pegou.
# NÃO inclui a palavra inglesa 'session' isolada DE PROPÓSITO: tracebacks Python trazem
# 'requests.sessions.Session' o tempo todo, e um blip de rede (503) cujo traceback cita
# 'Session' viraria escalar_reseed indevido. Todas as frases REAIS de sessão-morta em
# inglês (session expired/lost, not logged in, log in again) já estão em _RE_SESSAO.
# Aqui só 'login'/'logon' (raros num traceback benigno) e o 'sessão' PT do domínio.
_RE_SESSAO_FRACA = re.compile(r"\b(login|logon|sess[aã]o)\b", re.I)

# Credencial de API ruim (GROQ/401/403/api key). É o que escala TROCA DE TOKEN.
_RE_TOKEN = re.compile(
    r"(groq|x-api-key|api[_\s-]?key|authenticationerror|"
    r"\b40[13]\b|http\s*40[13]|status\s*40[13]|unauthorized|forbidden|"
    r"invalid\s+api\s+key|incorrect\s+api\s+key|insufficient[_\s]permission)", re.I)

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

# exit codes de SIGKILL: -9 (Popen) e 137 (128+9, via shell).
_EXIT_SIGKILL = {-9, 137}


def _deterministico(obito):
    """Devolve (acao, motivo) se bater numa assinatura conhecida; None se DESCONHECIDA."""
    err = obito.stderr_tail or ""
    code = obito.exit_code

    # 1) SESSÃO (precedência máxima): a causa-raiz de uma morte com sinal de sessão é
    #    sempre a sessão — relançar sem reseed reproduziria a morte.
    if _RE_SESSAO.search(err):
        return "escalar_reseed", "assinatura de sessão morta no stderr"

    # 2) TOKEN de API (GROQ/401/403/api key): credencial ruim, troca de chave.
    if _RE_TOKEN.search(err):
        return "escalar_token", "assinatura de credencial de API inválida no stderr"

    # 2b) 'login'/'session' isolado (sem os marcadores fortes de sessão nem de API):
    #     ainda é problema de sessão -> reseed (nunca login automático).
    if _RE_SESSAO_FRACA.search(err):
        return "escalar_reseed", "menção a sessão/login no stderr"

    # 3) SIGKILL / OOM / timeout -> relançar (transitório de recurso/SO; motor é idempotente).
    if code in _EXIT_SIGKILL:
        return "relancar", "exit code de SIGKILL (%s)" % code
    if _RE_OOM.search(err):
        return "relancar", "assinatura de OOM/falta de recurso no stderr"
    if _RE_TIMEOUT.search(err):
        return "relancar", "assinatura de timeout no stderr"

    # 4) TRANSITÓRIO de rede/servidor, ou saída LIMPA (a completude é do loop/Notion).
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


def classificar(obito, llm=None):
    """Classifica um `Obito` numa `Decisao`. `llm` é o seam do diagnosticador (callable
    prompt->texto); None => nenhum LLM disponível => fail-closed em escalar_humano quando
    a causa é desconhecida. Produção passa `llm=seam_claude_p`."""
    det = _deterministico(obito)
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
