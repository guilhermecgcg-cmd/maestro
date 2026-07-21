"""Interruptor MECÂNICO de re-disparo da captura, por PLATAFORMA ou por CONTA.

É o gatilho DESATIVAR do modelo doméstico. O ciclo de 120s da Athena, a cada
volta, passa a lista de cursos desejados por `filtrar_cursos(cursos, path)` ANTES
de decidir o que disparar. Um curso cuja plataforma (ou conta) esteja em
`controle.yaml` some da lista disparável — logo o ciclo NÃO o RE-dispara.

INVIOLÁVEL — Ordem IV (aditivo, nunca matar processo bom): pausar corta só o
RE-disparo. A captura que já está VIVA termina sozinha (o guard anti-ban do
LocalExecutor, em disco, continua sendo a verdade do que roda). Este módulo NÃO
toca em processo nenhum — só num arquivo YAML.

Duas decisões que fazem o interruptor ser CONFIÁVEL:

  1. Gravação ATÔMICA (os.replace): escreve num .tmp e faz rename atômico para o
     destino. Um leitor concorrente (o ciclo lendo enquanto o comando grava) nunca
     vê meio-arquivo; um crash no meio da escrita não corrompe o controle vigente.

  2. Corrupção é fail-LOUD, ausência é fail-open-para-ATIVO. Arquivo AUSENTE (1º
     boot) => nada pausado (a lista passa inteira; o never-stop não pode parar por
     falta de um arquivo opcional). Arquivo PRESENTE mas ilegível/formato errado =>
     LEVANTA. Nunca engolir: tratar corrupção como "nada pausado" ignoraria um
     pause deliberado do dono; tratá-la como "tudo pausado" mataria o never-stop.
     Levantar faz o ciclo doméstico ESCALAR honesto (ele já é fail-closed por-ciclo).
"""
import json
import os

import yaml

_CHAVES = ("plataformas_pausadas", "contas_pausadas")


def _norm_plat(p) -> str:
    """Plataforma casa sem depender de caixa nem de espaço em volta ('STOA' == 'stoa')."""
    return str(p or "").strip().lower()


def _norm_conta(c) -> str:
    """Conta é um identificador (perfil de Chrome dedicado) — casa exata, só sem
    espaço em volta (NÃO baixa a caixa: contas podem ser case-sensitive)."""
    return str(c or "").strip()


def carregar(path) -> dict:
    """Lê o controle.yaml -> {'plataformas_pausadas': [...], 'contas_pausadas': [...]}.

    Ausente/vazio => tudo ativo (listas vazias). Presente mas não-mapa, ou com uma
    chave que não é lista => LEVANTA ValueError (fail-LOUD; ver docstring do módulo).
    yaml.YAMLError (sintaxe quebrada) propaga do próprio safe_load."""
    try:
        with open(path) as f:
            bruto = yaml.safe_load(f)
    except FileNotFoundError:
        return {k: [] for k in _CHAVES}
    if bruto is None:
        return {k: [] for k in _CHAVES}
    if not isinstance(bruto, dict):
        raise ValueError(
            f"controle.yaml inválido: esperava um mapa, veio {type(bruto).__name__}")
    out = {}
    for k in _CHAVES:
        v = bruto.get(k, []) or []
        if not isinstance(v, list):
            raise ValueError(
                f"controle.yaml: '{k}' deve ser uma lista, veio {type(v).__name__}")
        out[k] = [str(x) for x in v]
    return out


def _salvar(path, estado) -> None:
    """Grava o estado de forma ATÔMICA: escreve+fsync num .tmp e faz os.replace
    (rename atômico) para o destino. Cria o diretório se preciso."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    dados = {k: sorted(dict.fromkeys(estado.get(k, []))) for k in _CHAVES}
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as f:
            yaml.safe_dump(dados, f, allow_unicode=True, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)                 # troca ATÔMICA (só aqui o destino muda)
    finally:
        # se o replace não rodou (ex.: falha antes), não deixa .tmp órfão para trás.
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# --- mutação (comandos) -----------------------------------------------------
def pausar_plataforma(path, plataforma) -> dict:
    """Marca a plataforma como pausada (idempotente: não duplica). Devolve o estado
    resultante já gravado."""
    est = carregar(path)
    plat = _norm_plat(plataforma)
    if plat and plat not in {_norm_plat(p) for p in est["plataformas_pausadas"]}:
        est["plataformas_pausadas"].append(plat)
    _salvar(path, est)
    return carregar(path)


def ativar_plataforma(path, plataforma) -> dict:
    """Remove a plataforma da lista de pausadas (reativa o re-disparo). Idempotente."""
    est = carregar(path)
    plat = _norm_plat(plataforma)
    est["plataformas_pausadas"] = [
        p for p in est["plataformas_pausadas"] if _norm_plat(p) != plat]
    _salvar(path, est)
    return carregar(path)


def pausar_conta(path, conta) -> dict:
    est = carregar(path)
    c = _norm_conta(conta)
    if c and c not in {_norm_conta(x) for x in est["contas_pausadas"]}:
        est["contas_pausadas"].append(c)
    _salvar(path, est)
    return carregar(path)


def ativar_conta(path, conta) -> dict:
    est = carregar(path)
    c = _norm_conta(conta)
    est["contas_pausadas"] = [
        x for x in est["contas_pausadas"] if _norm_conta(x) != c]
    _salvar(path, est)
    return carregar(path)


def plataforma_pausada(path, plataforma) -> bool:
    est = carregar(path)
    return _norm_plat(plataforma) in {_norm_plat(p) for p in est["plataformas_pausadas"]}


# --- leitura (gatilho do ciclo) --------------------------------------------
def filtrar_cursos(cursos, path) -> list:
    """Remove da lista os cursos cuja plataforma OU conta esteja pausada em
    `controle.yaml`. É o gatilho que o ciclo doméstico chama a cada volta: o que
    sai desta lista simplesmente NÃO é re-disparado (nada é morto)."""
    est = carregar(path)
    plats = {_norm_plat(p) for p in est["plataformas_pausadas"]}
    contas = {_norm_conta(c) for c in est["contas_pausadas"]}
    out = []
    for c in cursos:
        if _norm_plat(getattr(c, "plataforma", "")) in plats:
            continue
        if _norm_conta(getattr(c, "conta", "")) in contas:
            continue
        out.append(c)
    return out


# --- verdade dos lockfiles (o que está VIVO agora; bate com ps) ------------
def _pid_vivo(pid) -> bool:  # pragma: no cover — sonda de PID real do SO
    """os.kill(pid, 0) NÃO envia sinal; só sonda a existência. Espelha a semântica
    conservadora do LocalExecutor: PermissionError => existe (vivo); demais => morto."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def capturas_vivas(lock_dir, *, pid_vivo=None) -> list:
    """Lê os lockfiles do LocalExecutor em `lock_dir` e devolve os dicts
    {pid, course_url, conta} das capturas AINDA VIVAS — a mesma verdade-em-disco que
    o guard anti-ban usa. É o que faz o /status bater com o `ps`.

    - pid=None (lock de INTENÇÃO, gravado antes do spawn): a conta está OCUPADA mas o
      PID é desconhecido -> conta como VIVA (fail-closed, espelha o LocalExecutor).
    - lock ilegível/corrompido: ignorado (não deixa o /status quebrar).
    `pid_vivo` é injetável (os testes simulam PID vivo/morto sem um processo real)."""
    pid_vivo = pid_vivo or _pid_vivo
    try:
        nomes = sorted(os.listdir(lock_dir))
    except OSError:
        return []
    vivas = []
    for nome in nomes:
        if not nome.endswith(".lock"):
            continue
        try:
            with open(os.path.join(lock_dir, nome)) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        pid = data.get("pid")
        if pid is None or pid_vivo(pid):
            vivas.append(data)
    return vivas
