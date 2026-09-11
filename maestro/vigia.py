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

MORTE SÓ-DE-LOCK: a captura de uma encarnação ANTERIOR do loop (bounce do vigia externo,
restart do launchd, reboot do Mac) não tem fonte — o Popen morreu com o loop que a
disparou. O lock durável é a evidência; `stderr_path_de(conta)` devolve o .err da conta
(o tail desta morte) e o mtime do lock contra o `kern.boottime` diz se ela nasceu antes do
boot atual (`Obito.lock_antes_do_boot` — o reinício/desligamento do Mac a matou).
"""
import glob
import json
import os
import re
import time
from dataclasses import dataclass, asdict, replace
from datetime import datetime
from typing import Optional


_AUTOPSIA_DIR_PADRAO = os.path.join(
    os.path.expanduser("~"), ".athena-local", "autopsias")

_JANELA_FLAP_S = 30 * 60          # 30 min — a janela do flapping
_FLAP_MIN = 3                     # >= 3 mortes na janela = flapping
_STDERR_LINHAS = 40              # quantas linhas de tail do stderr guardar
# Folga entre o .err e o lock do MESMO disparo. O `_spawn_popen` TRUNCA o .err da conta
# ("wb") logo ANTES do Popen e o lock com o PID é gravado logo DEPOIS — o .err de um
# disparo nunca é mais velho que o lock dele além do tempo do Popen. Um .err mais velho
# que isso é de um run ANTERIOR (o tee falhou neste disparo e caiu no DEVNULL): não é a
# evidência desta morte e não entra na autópsia.
_ERR_FOLGA_S = 5.0


@dataclass(frozen=True)
class FonteFilho:
    """O que se sabe de um filho supervisionado neste ciclo.

    `exit_code`: código de saída REAL do processo (proc.returncode após wait()); None se
      ainda vivo/desconhecido — nesse caso a morte é aferida pela liveness do `pid`.
    `stderr_path`: arquivo onde o stderr do filho foi tee'd (lê-se o TAIL).
    `stderr_tail`: texto do stderr direto (tem precedência sobre o path — para quem já
      colheu o stderr em memória). O `LocalExecutor` copia a cauda NO REAP: o .err é por
      conta e TRUNCADO a cada disparo, então reler o path depois pode devolver o run
      SEGUINTE da conta (incidente 10/09: TimeoutError virou "causa desconhecida").
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
    como 'morte' e disparar o falso alarme 'FLAP: N mortes na janela'.

    `autopsia_path`: caminho do JSON de autópsia GRAVADO para esta morte ("" quando
    nada foi gravado — saída limpa). É o gancho da OBSERVABILIDADE: quem classifica a
    causa depois (athena_local._autopsiar_ciclo -> causa.classificar) usa este path
    para ANOTAR a Decisao (acao/motivo/fonte/plataforma) no MESMO arquivo — sem ele, a
    autópsia em disco fica cega ('causa desconhecida') mesmo com a causa classificada."""
    conta: str
    curso: str
    exit_code: Optional[int]
    stderr_tail: str
    flaps_na_janela: int
    ts: str = ""                 # ISO-8601 do momento da autópsia
    saida_limpa: bool = False    # exit_code == 0: encerrou LIMPO, NÃO é morte
    autopsia_path: str = ""      # JSON gravado desta morte (p/ anotar a causa depois)
    # MORTE SÓ-DE-LOCK (detectada pelo PID morto do lock durável, sem exit_code: a
    # captura foi disparada por uma encarnação ANTERIOR do loop, que levou o Popen
    # junto). `lock_mtime` = quando o lock foi gravado (= o disparo); `boot_ts` = o
    # kern.boottime do Mac. `lock_antes_do_boot` => a captura nasceu ANTES do boot
    # atual: nenhum processo atravessa um reboot, então ela morreu (no máximo) no
    # reinício/desligamento do Mac. None/False em qualquer outra morte.
    lock_mtime: Optional[float] = None
    boot_ts: Optional[float] = None
    lock_antes_do_boot: bool = False


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
    """Varre lock_dir e devolve {conta: {pid, course_url, ..., _mtime}} do CONTEÚDO de
    cada .lock (o nome do arquivo é hash de mão-única da conta; a conta legível está no
    conteúdo). `_mtime` = mtime do arquivo (a hora do disparo; o lock não é regravado
    durante a captura) — None se o stat falhar."""
    out = {}
    if not lock_dir or not os.path.isdir(lock_dir):
        return out
    for path in glob.glob(os.path.join(lock_dir, "*.lock")):
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue                     # lock corrompido/ilegível: ignora (não trava)
        if not isinstance(data, dict):
            continue
        conta = data.get("conta")
        if conta is not None:
            try:
                data["_mtime"] = os.path.getmtime(path)
            except OSError:
                data["_mtime"] = None
            out[str(conta)] = data
    return out


# --------------------------------------------------------------------------
# boot do Mac — `sysctl kern.boottime` (a morte só-de-lock ANTERIOR ao boot é o reinício)
# --------------------------------------------------------------------------
_BOOT_TS_CACHE = []                  # [epoch] após a 1ª leitura BEM-SUCEDIDA (o boot não
                                     # muda durante a vida do processo do loop)
_RE_BOOTTIME = re.compile(r"sec\s*=\s*(\d+)(?:\s*,\s*usec\s*=\s*(\d+))?")


def boot_ts_do_mac():
    """Epoch (s) do boot ATUAL do Mac, lido de `sysctl -n kern.boottime` ("{ sec = N,
    usec = M } <data>"). None se indisponível (não-macOS, sysctl ausente/erro) — e aí a
    regra do reinício simplesmente não se aplica (fail-safe: cai no comportamento de
    sempre). NÃO se estima por time.time()-monotonic(): no macOS o relógio monotônico
    PARA durante o sono, e o boot estimado andaria para a frente a cada soneca."""
    if _BOOT_TS_CACHE:
        return _BOOT_TS_CACHE[0]
    import subprocess
    for exe in ("/usr/sbin/sysctl", "sysctl"):
        try:
            saida = subprocess.run([exe, "-n", "kern.boottime"], capture_output=True,
                                   text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        m = _RE_BOOTTIME.search(saida or "")
        if m:
            valor = float(m.group(1)) + (float(m.group(2)) / 1e6 if m.group(2) else 0.0)
            _BOOT_TS_CACHE.append(valor)
            return valor
    return None


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
        if rec.get("sem_falha"):
            continue                      # anotada pela causa como SEM FALHA da captura
                                          # (reinício do Mac / órfã que saiu limpa)
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
    rec.pop("autopsia_path", None)        # o arquivo não aponta para si mesmo
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
def _err_desta_morte(stderr_path_de, conta, lock_mtime):
    """O .err da conta (onde o `_spawn_popen` tee'a o motor) SE ele é do MESMO disparo do
    lock — senão None. O .err é TRUNCADO a cada disparo da conta e o lock é regravado no
    mesmo disparo, então enquanto o lock morto está no disco o .err ainda é o do run que
    morreu (a autópsia roda ANTES da passada que re-dispara e trunca). A única forma de
    ele ser de OUTRO run é ser mais VELHO que o lock (tee falhou no disparo): descartado.
    Best-effort: qualquer erro => None (a morte segue sem stderr, como antes)."""
    try:
        path = stderr_path_de(conta)
        if not path:
            return None
        err_mtime = os.path.getmtime(path)
    except Exception:
        return None
    if lock_mtime is None or err_mtime < lock_mtime - _ERR_FOLGA_S:
        return None
    return path


def autopsia(lock_dir, stderr_por_conta, *, pid_vivo=None, agora=None,
             autopsia_dir=None, janela_s=_JANELA_FLAP_S, flap_min=_FLAP_MIN,
             stderr_linhas=_STDERR_LINHAS, stderr_path_de=None, boot_ts=None):
    """Detecta filhos MORTOS cruzando os locks em `lock_dir` com `stderr_por_conta`
    (conta -> FonteFilho|dict|Popen|path). Grava uma autópsia por morte em `autopsia_dir`
    e devolve a lista de `Obito` (uma por conta morta), com o flap da janela preenchido.

    `flap_min` não filtra nada aqui — todas as mortes são reportadas; `flap_min` é só a
    referência que a causa/never-stop usa para decidir que a conta está flapando. O VIGIA
    devolve o NÚMERO (`flaps_na_janela`); quem interpreta o limiar é o chamador.

    MORTE SÓ-DE-LOCK (PID do lock morto, sem exit_code — captura de uma encarnação
    ANTERIOR do loop, cujo Popen se perdeu com ela; o `drenar_obitos` só conhece os
    filhos da encarnação atual):
      - `stderr_path_de(conta) -> path` (o `LocalExecutor._stderr_path`): a autópsia lê
        o .err da conta — sem ele a cauda era VAZIA e toda morte dessas virava "causa
        desconhecida (fail-closed)". Só se o .err for do mesmo disparo do lock
        (ver `_err_desta_morte`).
      - `boot_ts` (epoch do boot do Mac; None => `boot_ts_do_mac()`, lido só se houver
        morte assim): lock gravado ANTES do boot => `Obito.lock_antes_do_boot`.
    """
    pid_vivo = pid_vivo or _pid_vivo
    agora = time.time() if agora is None else agora
    autopsia_dir = autopsia_dir or _AUTOPSIA_DIR_PADRAO
    stderr_por_conta = stderr_por_conta or {}
    boot = {"ts": boot_ts, "lido": boot_ts is not None}

    def _boot():
        if not boot["lido"]:
            boot["ts"], boot["lido"] = boot_ts_do_mac(), True
        return boot["ts"]

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

        lock_mtime = boot_do_mac = None
        lock_antes_do_boot = False
        if detectado_por == "pid" and lock is not None:
            lock_mtime = lock.get("_mtime")
            if lock_mtime is None and isinstance(lock.get("ts"), (int, float)):
                lock_mtime = float(lock["ts"])
            if (stderr_path_de is not None and fonte.stderr_tail is None
                    and not fonte.stderr_path):
                err_path = _err_desta_morte(stderr_path_de, conta, lock_mtime)
                if err_path:
                    fonte = replace(fonte, stderr_path=err_path)
            boot_do_mac = _boot()
            lock_antes_do_boot = (lock_mtime is not None and boot_do_mac is not None
                                  and lock_mtime < boot_do_mac)

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
                      stderr_tail=stderr_tail, flaps_na_janela=flaps, ts=ts_iso,
                      lock_mtime=lock_mtime, boot_ts=boot_do_mac,
                      lock_antes_do_boot=lock_antes_do_boot)
        path = _gravar_autopsia(autopsia_dir, obito, ts_epoch=agora,
                                detectado_por=detectado_por)
        # O Obito devolvido CARREGA o path da autópsia gravada — é o gancho para o
        # chamador anotar a causa classificada no MESMO arquivo (observabilidade).
        obitos.append(replace(obito, autopsia_path=path))
    return obitos


def _por_conta(mapa, conta):
    """Busca a fonte por conta, tolerando chaves não-str (a conta pode ser int/obj)."""
    if conta in mapa:
        return mapa[conta]
    for k, v in mapa.items():
        if str(k) == conta:
            return v
    return None
