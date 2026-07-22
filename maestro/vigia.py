"""VIGIA — a AUTÓPSIA dos filhos que a Athena spawna.

O `captura.LocalExecutor` dispara o MOTOR como subprocesso e grava, por conta, um lock
DURÁVEL em ~/.athena-local/locks/<sha256(conta)[:16]>.lock com {pid, course_url, conta}.
Quando um filho MORRE (crash, kill, OOM, fim), o VIGIA faz a autópsia: cruza o lock (quem
DEVIA rodar) com a FONTE de stderr do filho (código de saída + últimas linhas do stderr),
produz um `Obito`, detecta FLAPPING (>= `flap_min` mortes na `janela_s` na MESMA conta) e
grava a autópsia em ~/.athena-local/autopsias/<ts>-<conta>.json — para o never-stop e o
brainstorm autônomo (causa.py) lerem depois.

FRONTEIRAS (o que o VIGIA NÃO faz, de propósito):
  - NÃO decide o que fazer com a morte — isso é `causa.classificar`.
  - NÃO mata nem re-dispara nada (Ordem IV: aditivo; o VIGIA só OBSERVA e REGISTRA).
  - NÃO sonda a plataforma nem o Notion — a completude/never-stop é do owner.

CONTRATO COM O CHAMADOR (idempotência entre ciclos): o VIGIA reporta as mortes PRESENTES
nas entradas deste ciclo. Um filho já autopsiado NÃO deve ser re-alimentado no ciclo
seguinte, senão infla o flap: na prática, o `LocalExecutor._reap` remove o lock do filho
encerrado e o daemon descarta a fonte após agir — de modo que o mesmo óbito não volta.

FONTE de stderr/código: um filho é observado por uma `FonteFilho` (ou um dict/objeto
equivalente). O `exit_code` (código de saída REAL do processo, ex.: `proc.returncode`
após `wait()`) é o sinal FORTE de morte; na sua ausência, sonda-se a liveness do PID
(do lock ou da fonte). O stderr do filho é lido de um arquivo (`stderr_path`, onde o
spawn tee'a o stderr) ou fornecido direto (`stderr_tail`).
"""
import glob
import json
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional


_AUTOPSIA_DIR_PADRAO = os.path.join(
    os.path.expanduser("~"), ".athena-local", "autopsias")

_JANELA_FLAP_S = 30 * 60          # 30 min — a janela do flapping
_FLAP_MIN = 3                     # >= 3 mortes na janela = flapping
_STDERR_LINHAS = 40              # quantas linhas de tail do stderr guardar


@dataclass(frozen=True)
class FonteFilho:
    """O que se sabe de um filho supervisionado neste ciclo.

    `exit_code`: código de saída REAL do processo (proc.returncode após wait()); None se
      ainda vivo/desconhecido — nesse caso a morte é aferida pela liveness do `pid`.
    `stderr_path`: arquivo onde o stderr do filho foi tee'd (lê-se o TAIL).
    `stderr_tail`: texto do stderr direto (tem precedência sobre o path — para quem já
      colheu o stderr em memória).
    `pid`: pid do filho (para sondar liveness quando `exit_code` é None). Se ausente,
      cai no pid do lock.
    `curso`: URL do curso (fallback quando NÃO há lock em disco).
    """
    exit_code: Optional[int] = None
    stderr_path: Optional[str] = None
    stderr_tail: Optional[str] = None
    pid: Optional[int] = None
    curso: Optional[str] = None


@dataclass(frozen=True)
class Obito:
    """Uma morte de filho, com o material da autópsia. `flaps_na_janela` é quantas mortes
    (INCLUINDO esta) a conta acumulou na janela — >= flap_min é flapping.

    `saida_limpa`: True quando o filho encerrou LIMPO (exit_code == 0). NÃO é uma morte —
    é um run que terminou sem erro (curso concluído / nada pendente / fim de passe). Um
    `Obito` com `saida_limpa` é REPORTADO (o loop precisa do sinal de que o processo
    encerrou), mas NÃO é um evento de flap: não conta na janela nem grava autópsia (o
    diretório de autópsias é de MORTES). Distinguir os dois é o que impede um curso já
    100% capturado — que a cada disparo injeta a sessão, roda e sai 0 — de ser contado
    como 'morte' e disparar o falso alarme 'FLAP: N mortes na janela'."""
    conta: str
    curso: str
    exit_code: Optional[int]
    stderr_tail: str
    flaps_na_janela: int
    ts: str = ""                 # ISO-8601 do momento da autópsia
    saida_limpa: bool = False    # exit_code == 0: encerrou LIMPO, NÃO é morte


# --------------------------------------------------------------------------
# liveness do PID — replicado (de propósito) de captura._pid_vivo, para o VIGIA
# não acoplar à cadeia de imports do executor (que outro trecho está mexendo).
# --------------------------------------------------------------------------
def _pid_vivo(pid) -> bool:
    """O PID ainda roda? `os.kill(pid, 0)` NÃO envia sinal — só sonda a existência.
    ProcessLookupError => morto. PermissionError => existe mas de outro dono (VIVO,
    conservador). pid inválido (None/<=0) => morto. Qualquer outro OSError => morto
    (fail-open p/ não travar para sempre)."""
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


# --------------------------------------------------------------------------
# leitura das entradas
# --------------------------------------------------------------------------
def _coerce_fonte(fonte) -> FonteFilho:
    """Normaliza uma entrada de `stderr_por_conta` para `FonteFilho`. Aceita: FonteFilho,
    dict (subset das chaves), str/PathLike (tratado como stderr_path), None, ou um objeto
    Popen-like (tem `.poll()`/`.returncode`/`.pid`)."""
    if fonte is None:
        return FonteFilho()
    if isinstance(fonte, FonteFilho):
        return fonte
    if isinstance(fonte, dict):
        campos = {"exit_code", "stderr_path", "stderr_tail", "pid", "curso"}
        return FonteFilho(**{k: v for k, v in fonte.items() if k in campos})
    if isinstance(fonte, (str, bytes, os.PathLike)):
        return FonteFilho(stderr_path=os.fspath(fonte))
    # Popen-like: refresca o returncode via poll() e colhe o que der.
    if hasattr(fonte, "poll") or hasattr(fonte, "returncode"):
        try:
            fonte.poll()
        except Exception:
            pass
        return FonteFilho(exit_code=getattr(fonte, "returncode", None),
                          pid=getattr(fonte, "pid", None))
    raise TypeError("fonte de stderr desconhecida: %r" % (type(fonte),))


def _ler_locks(lock_dir) -> dict:
    """Varre lock_dir e devolve {conta: {pid, course_url}} do CONTEÚDO de cada .lock
    (o nome do arquivo é hash de mão-única da conta; a conta legível está no conteúdo)."""
    out = {}
    if not lock_dir or not os.path.isdir(lock_dir):
        return out
    for path in glob.glob(os.path.join(lock_dir, "*.lock")):
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue                     # lock corrompido/ilegível: ignora (não trava)
        conta = data.get("conta")
        if conta is not None:
            out[str(conta)] = data
    return out


def _stderr_tail(fonte: FonteFilho, n_linhas: int) -> str:
    """Últimas `n_linhas` do stderr do filho. `stderr_tail` explícito vence o path."""
    texto = fonte.stderr_tail
    if texto is None and fonte.stderr_path:
        try:
            with open(fonte.stderr_path, errors="replace") as f:
                texto = f.read()
        except OSError:
            texto = ""
    if not texto:
        return ""
    linhas = texto.splitlines()
    return "\n".join(linhas[-n_linhas:])


# --------------------------------------------------------------------------
# flapping — contado a partir das autópsias em disco (recomputável, never-stop)
# --------------------------------------------------------------------------
def _contar_flaps_anteriores(autopsia_dir, conta, agora, janela_s) -> int:
    """Quantas MORTES já gravadas desta conta caem em [agora-janela, agora]. NÃO conta
    a atual (ainda não escrita).

    IGNORA registros de SAÍDA LIMPA (`saida_limpa` True ou `exit_code == 0`): um exit 0 é
    um encerramento sem erro, não uma morte, e não pode inflar o flap. O filtro por
    `exit_code == 0` também neutraliza as autópsias exit-0 LEGADAS que o bug antigo já
    deixou no disco (dezenas), para uma morte real não herdar um flap falso delas."""
    if not os.path.isdir(autopsia_dir):
        return 0
    limite = agora - janela_s
    n = 0
    for path in glob.glob(os.path.join(autopsia_dir, "*.json")):
        try:
            with open(path) as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if str(rec.get("conta")) != str(conta):
            continue
        if rec.get("saida_limpa") or rec.get("exit_code") == 0:
            continue                      # saída limpa não é morte -> não conta flap
        ts = rec.get("ts_epoch")
        if isinstance(ts, (int, float)) and limite <= ts <= agora:
            n += 1
    return n


def _slug(conta) -> str:
    seguro = "".join(c if c.isalnum() else "-" for c in str(conta))
    return seguro[:40] or "conta"


def _gravar_autopsia(autopsia_dir, obito: Obito, ts_epoch: float, detectado_por: str):
    os.makedirs(autopsia_dir, exist_ok=True)
    rec = asdict(obito)
    rec["ts_epoch"] = ts_epoch
    rec["detectado_por"] = detectado_por
    carimbo = datetime.fromtimestamp(ts_epoch).strftime("%Y%m%dT%H%M%S_%f")
    nome = "%s-%s.json" % (carimbo, _slug(obito.conta))
    path = os.path.join(autopsia_dir, nome)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)                 # troca atômica: nunca uma autópsia meio-escrita
    return path


# --------------------------------------------------------------------------
# a autópsia
# --------------------------------------------------------------------------
def autopsia(lock_dir, stderr_por_conta, *, pid_vivo=None, agora=None,
             autopsia_dir=None, janela_s=_JANELA_FLAP_S, flap_min=_FLAP_MIN,
             stderr_linhas=_STDERR_LINHAS):
    """Detecta filhos MORTOS cruzando os locks em `lock_dir` com `stderr_por_conta`
    (conta -> FonteFilho|dict|Popen|path). Grava uma autópsia por morte em `autopsia_dir`
    e devolve a lista de `Obito` (uma por conta morta), com o flap da janela preenchido.

    `flap_min` não filtra nada aqui — todas as mortes são reportadas; `flap_min` é só a
    referência que a causa/never-stop usa para decidir que a conta está flapando. O VIGIA
    devolve o NÚMERO (`flaps_na_janela`); quem interpreta o limiar é o chamador.
    """
    pid_vivo = pid_vivo or _pid_vivo
    agora = time.time() if agora is None else agora
    autopsia_dir = autopsia_dir or _AUTOPSIA_DIR_PADRAO
    stderr_por_conta = stderr_por_conta or {}

    locks = _ler_locks(lock_dir)
    contas = set(locks) | set(str(c) for c in stderr_por_conta)

    obitos = []
    for conta in sorted(contas):
        fonte = _coerce_fonte(_por_conta(stderr_por_conta, conta))
        lock = locks.get(conta)
        pid = fonte.pid if fonte.pid is not None else (
            lock.get("pid") if lock else None)

        if fonte.exit_code is not None:
            detectado_por = "exit_code"           # sinal FORTE: processo encerrou
        elif pid is not None:
            if pid_vivo(pid):
                continue                          # ainda roda -> quieto (MANTER)
            detectado_por = "pid"                 # PID sumiu -> morreu
        else:
            # lock de intenção (pid=None) ou sem qualquer sinal: NÃO se afirma morte
            # (fail-safe — nunca inventar um óbito que dispara escalonamento à toa).
            continue

        curso = fonte.curso or (lock.get("course_url") if lock else "") or ""
        stderr_tail = _stderr_tail(fonte, stderr_linhas)
        ts_iso = datetime.fromtimestamp(agora).isoformat()

        # SAÍDA LIMPA (exit_code == 0): o filho encerrou SEM erro — NÃO é morte. É o run de
        # um curso já concluído / passe sem nada pendente. Reporta-se o Obito (o loop
        # precisa saber que o processo encerrou, p/ cooldown/conclusão), mas: (a) o flap
        # dela é o das MORTES anteriores (SEM +1 — saída limpa não é evento de flap); e
        # (b) NÃO se grava autópsia (o diretório é de mortes, e gravar exit-0 é o que
        # inflava o flap e produzia o falso 'FLAP: N mortes na janela'). É o ponto exato
        # onde, no bug, uma saída de motor virava 'morte' no contador de FLAP.
        if fonte.exit_code == 0:
            flaps = _contar_flaps_anteriores(autopsia_dir, conta, agora, janela_s)
            obitos.append(Obito(conta=conta, curso=curso, exit_code=0,
                                stderr_tail=stderr_tail, flaps_na_janela=flaps,
                                ts=ts_iso, saida_limpa=True))
            continue

        flaps = _contar_flaps_anteriores(autopsia_dir, conta, agora, janela_s) + 1
        obito = Obito(conta=conta, curso=curso, exit_code=fonte.exit_code,
                      stderr_tail=stderr_tail, flaps_na_janela=flaps, ts=ts_iso)
        _gravar_autopsia(autopsia_dir, obito, ts_epoch=agora, detectado_por=detectado_por)
        obitos.append(obito)
    return obitos


def _por_conta(mapa, conta):
    """Busca a fonte por conta, tolerando chaves não-str (a conta pode ser int/obj)."""
    if conta in mapa:
        return mapa[conta]
    for k, v in mapa.items():
        if str(k) == conta:
            return v
    return None
