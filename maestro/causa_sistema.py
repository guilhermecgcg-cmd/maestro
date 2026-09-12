"""CAUSA_SISTEMA — o conjunto-irmão FECHADO de `causa.py`, para a morte de um RUN
de sistema gerado (não de uma captura).

Mesmo desenho de `causa.py` (determinístico-primeiro sobre `(exit_code, stderr_tail)`,
seam LLM opt-in e recusável, fail-closed → `escalar_humano`), com uma diferença
DURA e deliberada:

  **`escalar_reseed` NÃO EXISTE aqui.** Um sistema gerado NÃO tem sessão de
  plataforma (invariante 1 da spec-mãe: sistemas consomem biblioteca/Notion, nunca
  a plataforma raspada). Não há superfície de ban a proteger — então matar um run
  travado é SEGURO (≠ captura) e nenhuma morte vira "refaça o login". No lugar do
  reseed entra `acionar_engenharia` (re-rodar o Construtor, a única porta de
  mudança de código — F4-e).

Conjunto FECHADO de ações:
  nada             — exit 0/10/20: desfecho normal, tratado pelo resultado.json/ledger.
  relancar         — SIGKILL/OOM/timeout: transitório de recurso/SO, re-dispara.
  aguardar_backoff — transitório de rede/servidor: espera e o loop reavalia.
  acionar_engenharia — exit 30 (PlanoInvalido) / exit 40 / traceback: defeito de
                       plano ou de execução → re-rodar o Construtor (F4-e).
  pausar_sistema   — proposta possível do LLM (custo anômalo etc.); nunca determinística.
  escalar_token    — 401/403/api-key: humano troca a chave (backoff, nunca latch).
  escalar_humano   — desconhecida / fail-closed: chama o dono.

Um LLM que proponha algo FORA deste conjunto — inclusive `escalar_reseed`,
`relogar` ou login automático — é RECUSADO (vira escalar_humano).
"""
import re
from dataclasses import dataclass

# Exit codes do entrypoint `sintetizador.rodar_sistema` (contrato FECHADO F4-c).
EXIT_SUCESSO = 0
EXIT_PARCIAL = 10
EXIT_CONGELADO = 20
EXIT_PLANO_INVALIDO = 30
EXIT_CRASH = 40

# Conjunto FECHADO de ações. `escalar_reseed`/`relogar` NÃO estão aqui — de propósito
# (sistema não tem sessão; não há reautenticação a pedir).
ACOES = {
    "nada",
    "relancar",
    "aguardar_backoff",
    "acionar_engenharia",
    "pausar_sistema",
    "escalar_token",
    "escalar_humano",
}

# O LLM só pode devolver uma destas.
_ACOES_LLM = set(ACOES)

# Tokens que o LLM JAMAIS pode nos fazer executar (login automático) E o reseed —
# que aqui é PROIBIDO por construção (sistema não tem sessão).
_PROIBIDOS = ("relogar", "relogin", "auto-login", "autologin", "auto login",
              "login automatico", "login automático", "escalar_reseed", "reseed")


@dataclass(frozen=True)
class Decisao:
    acao: str
    motivo: str = ""
    fonte: str = ""


# --- assinaturas determinísticas (herdadas de causa.py; SEM _RE_SESSAO) ------
# ATENÇÃO (r15): esta é a CÓPIA da regex de token que a `causa.py` tinha até 12/09 —
# com `unauthorized`/`forbidden` NUAS. Na lane de CAPTURA essa largura produziu 23
# falsos "troque o token" em 7 dias (todos o 403 da CDN do yt-dlp), e lá ela foi
# substituída por `causa._sinal_de_credencial` (contexto de chave de API obrigatório).
# AQUI ela CONTINUA como estava, de propósito: a lane de SISTEMAS é opt-in
# (ATHENA_SISTEMAS_PATH) e não tem NENHUM falso medido — apertar sem evidência seria
# trocar um risco conhecido por um desconhecido. Se um sistema gerado começar a escalar
# token à toa, a correção já existe pronta em `causa._sinal_de_credencial`: importe-a.
_RE_TOKEN = re.compile(
    r"(authenticationerror|permissiondeniederror|unauthorized|forbidden|"
    r"http\s*40[13]\b|status\s*40[13]\b|"
    r"invalid\s+api[_\s-]?key|incorrect\s+api[_\s-]?key|"
    r"api[_\s-]?key\s+(invalid|incorrect|missing|expired|revoked|not\s+found)|"
    r"(invalid|incorrect|missing|expired|revoked)\s+api[_\s-]?key|"
    r"insufficient[_\s]permission)", re.I)

_RE_OOM = re.compile(
    r"(out\s+of\s+memory|oom[-_\s]?kill|memoryerror|cannot\s+allocate|"
    r"killed\s+process|no\s+space\s+left)", re.I)
_RE_TIMEOUT = re.compile(r"(time(d)?\s*out|timeouterror|timeout)", re.I)
_RE_TRACEBACK = re.compile(r"(traceback\s*\(most\s+recent\s+call\s+last\)|"
                           r"\w+error:|\w+exception:)", re.I)
_RE_TRANSITORIO = re.compile(
    r"(connection\s+reset|connectionreseterror|connection\s+refused|"
    r"temporarily\s+unavailable|try\s+again|\b50[0234]\b|service\s+unavailable|"
    r"bad\s+gateway|gateway\s+timeout|econnreset|network\s+is\s+unreachable)", re.I)

_EXIT_SIGKILL = {-9, 137}
_EXIT_NORMAL = {EXIT_SUCESSO, EXIT_PARCIAL, EXIT_CONGELADO}


def _deterministico(obito):
    """(acao, motivo) se bate numa assinatura; None se DESCONHECIDA."""
    err = getattr(obito, "stderr_tail", "") or ""
    code = getattr(obito, "exit_code", None)

    # 1) TOKEN de API (401/403/api key ruim): humano troca a chave (nunca latcha).
    if _RE_TOKEN.search(err):
        return "escalar_token", "assinatura de credencial de API inválida no stderr"

    # 2) Contrato de EXIT-CODE do entrypoint (sinal FORTE):
    #    0/10/20 = desfecho NORMAL (o resultado.json/ledger trata; não é morte a diagnosticar).
    if code in _EXIT_NORMAL:
        return "nada", f"exit {code}: desfecho normal (tratado pelo resultado.json)"
    #    30 = PlanoInvalido → defeito de plano → engenharia (re-rodar o Construtor).
    if code == EXIT_PLANO_INVALIDO:
        return "acionar_engenharia", "exit 30 (PlanoInvalido): defeito de plano"
    #    40 = crash de infra/exceção não tratada → engenharia.
    if code == EXIT_CRASH:
        return "acionar_engenharia", "exit 40 (crash): exceção não tratada no run"

    # 3) SIGKILL / OOM / timeout → relançar (transitório de recurso/SO; o runtime é idempotente).
    if code in _EXIT_SIGKILL:
        return "relancar", "exit code de SIGKILL (%s)" % code
    if _RE_OOM.search(err):
        return "relancar", "assinatura de OOM/falta de recurso no stderr"
    if _RE_TIMEOUT.search(err):
        return "relancar", "assinatura de timeout no stderr"

    # 4) Transitório de rede/servidor → aguardar backoff.
    if _RE_TRANSITORIO.search(err):
        return "aguardar_backoff", "assinatura de transitório de rede/servidor no stderr"

    # 5) Traceback SEM exit-code de contrato → engenharia (defeito de execução).
    if _RE_TRACEBACK.search(err):
        return "acionar_engenharia", "traceback no stderr (defeito de execução)"

    return None                        # DESCONHECIDA → seam do LLM


def _prompt(obito) -> str:
    return (
        "Você é o diagnosticador de causa-raiz da Athena para a morte de um RUN de "
        "SISTEMA GERADO (não uma captura). Classifique a causa e escolha UMA ação do "
        "conjunto FECHADO, respondendo APENAS com um JSON {\"acao\": \"<uma-das-ações>\"}.\n\n"
        "Ações permitidas (e SÓ estas):\n"
        "  - nada: desfecho normal, nada a fazer.\n"
        "  - relancar: transitório de recurso/SO; basta re-disparar o run.\n"
        "  - aguardar_backoff: transitório de rede/servidor; esperar e reavaliar.\n"
        "  - acionar_engenharia: defeito de plano/execução; re-rodar o Construtor.\n"
        "  - pausar_sistema: custo/comportamento anômalo; pausar até inspeção humana.\n"
        "  - escalar_token: credencial de API inválida.\n"
        "  - escalar_humano: não dá para classificar com segurança.\n\n"
        "REGRA INVIOLÁVEL: um sistema gerado NÃO tem sessão de plataforma. NUNCA "
        "proponha reseed nem login automático — não existe sessão a refazer.\n\n"
        "Dados do óbito:\n  slug: %s\n  exit_code: %s\n  stderr (tail):\n%s\n" % (
            getattr(obito, "conta", getattr(obito, "curso", "?")),
            getattr(obito, "exit_code", None),
            getattr(obito, "stderr_tail", "") or "(vazio)"))


def seam_claude_p(prompt: str) -> str:  # pragma: no cover — subprocesso REAL do claude
    import subprocess
    r = subprocess.run(["claude", "-p", prompt], capture_output=True, text=True,
                       timeout=120)
    return r.stdout


def _parse_acao_llm(resp: str):
    if not resp:
        return None
    baixo = resp.lower()
    m = re.search(r'"acao"\s*:\s*"([a-z_]+)"', baixo)
    if m:
        cand = m.group(1)
        # Recusa explícita de reseed/login mesmo se vier bem-formatado.
        if cand not in _ACOES_LLM or any(p in cand for p in _PROIBIDOS):
            return None
        return cand
    achados = [a for a in _ACOES_LLM if a in baixo]
    if any(p in baixo for p in _PROIBIDOS) and len(achados) != 1:
        return None
    if len(achados) == 1 and not any(p in baixo for p in _PROIBIDOS):
        return achados[0]
    return None


def classificar(obito, llm=None):
    """Classifica um óbito de RUN de sistema numa `Decisao` do conjunto FECHADO.
    `llm` é o seam opt-in; None → fail-closed em escalar_humano quando a causa é
    desconhecida. NUNCA devolve `escalar_reseed` (não existe aqui)."""
    det = _deterministico(obito)
    if det is not None:
        acao, motivo = det
        return Decisao(acao=acao, motivo=motivo, fonte="deterministico")

    if llm is None:
        return Decisao(acao="escalar_humano",
                       motivo="causa desconhecida e nenhum LLM disponível",
                       fonte="fail-closed")
    try:
        resp = llm(_prompt(obito))
    except Exception as e:
        return Decisao(acao="escalar_humano",
                       motivo="LLM falhou: %s" % e, fonte="fail-closed")

    acao = _parse_acao_llm(resp if isinstance(resp, str) else str(resp))
    if acao is None or acao not in ACOES:
        return Decisao(acao="escalar_humano",
                       motivo="resposta do LLM fora do conjunto fechado (ou reseed recusado)",
                       fonte="fail-closed")
    return Decisao(acao=acao, motivo="classificado pelo LLM", fonte="llm")
