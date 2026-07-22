"""Adaptador de SISTEMA GERADO: a Athena supervisiona um sistema-fábrica como um
ALVO ao lado da captura — MESMO loop, MESMOS vigia/causa/disjuntor, injetados pelo
MESMO padrão null-object aditivo.

`SistemaExecutor` ESPELHA o contrato do `captura.LocalExecutor` (disparar /
<alvo>_ativo / drenar_obitos + lock durável + tee de stderr), para que a autópsia
(vigia) e a máquina de never-stop funcionem SEM mudança — a chave de serialização
passa de `conta` (anti-ban) para `slug` (1 run por sistema por vez).

DIFERENÇAS DELIBERADAS vs. a captura (spec-mãe / contrato F4 §2.2, §3.2):
  - NÃO há sessão de plataforma → NÃO há anti-ban → matar um run TRAVADO é SEGURO
    (`matar`, chamado quando o heartbeat congela). A captura JAMAIS mata (Ordem IV);
    aqui matar é a ação correta (nenhuma superfície de ban, nenhum reseed).
  - O tee do stderr entra desde o dia 1 (a autópsia de sistema depende do stderr —
    o entrypoint imprime tracebacks lá).
  - A integração é por ARQUIVO: dispara `ATHENA_FABRICA_PYTHON -m
    sintetizador.rodar_sistema <raiz>` como SUBPROCESSO (cwd=ATHENA_FABRICA_DIR) e
    lê heartbeat/ledger/resultado.json — NUNCA importa `sintetizador.*`.
"""
import json
import os
import signal
import time
from dataclasses import dataclass, field


class SistemaOcupado(RuntimeError):
    """Levantada ao tentar disparar um 2º run do MESMO sistema enquanto outro roda.
    NÃO é falha — é a serialização 1-run-por-slug (o disparo idempotente já trata o
    caso do MESMO slug; esta cobre a corrida). Sistemas DIFERENTES rodam em paralelo."""


@dataclass(frozen=True)
class SistemaSpec:
    """Um sistema gerado registrado como alvo supervisionado (análogo ao CursoLocal).
    `raiz` é a pasta do sistema (plano/, executores/, estado/). `entrada_inicial` e
    `calibrados` são materializados em JSON no disparo (o entrypoint os lê por path).
    `cadencia`='sob_demanda' (default) — cada run CUSTA; não se spawna em loop."""
    slug: str
    raiz: str
    estado: str = "ativo"            # ativo | pausado (semântica do controle P5)
    cadencia: str = "sob_demanda"    # sob_demanda | a_cada_horas:N
    entrada_inicial: dict = field(default_factory=dict)
    calibrados: dict = field(default_factory=dict)
    teto_dia_usd: float = 5.0
    precisa_24x7: bool = False


def _pid_vivo(pid) -> bool:
    """O PID ainda roda? (replicado de captura/vigia p/ não acoplar imports). pid
    inválido → morto; PermissionError → vivo (conservador); outro OSError → morto."""
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


_LOCK_DIR_PADRAO = os.path.join(
    os.path.expanduser("~"), ".athena-local", "locks-sistemas")


def _spawn_popen(cmd, *, env, cwd, stderr_path):  # pragma: no cover — processo REAL
    """Spawn REAL não-bloqueante do run. `start_new_session` desacopla o run do
    processo da Athena (um restart do loop NÃO mata um run em andamento — Ordem IV).
    stdout+stderr tee'd para `stderr_path` (truncado a cada disparo: o TAIL é do run
    atual). A autópsia lê esse arquivo."""
    import subprocess
    saida = subprocess.DEVNULL
    if stderr_path:
        try:
            os.makedirs(os.path.dirname(stderr_path), exist_ok=True)
            saida = open(stderr_path, "wb")
        except OSError:
            saida = subprocess.DEVNULL
    try:
        return subprocess.Popen(cmd, env=env, cwd=cwd, stdout=saida,
                                stderr=subprocess.STDOUT, start_new_session=True)
    finally:
        if saida not in (subprocess.DEVNULL, None):
            try:
                saida.close()
            except OSError:
                pass


class SistemaExecutor:
    """Espelha o contrato do LocalExecutor para sistemas gerados. Métodos-contrato:
    `disparar(slug) -> confirmação|LEVANTA`, `sistema_ativo(slug) -> bool`,
    `drenar_obitos() -> {slug: fonte}`, `matar(slug) -> bool` (kill SEGURO do run
    travado — exclusivo do sistema, jamais da captura)."""

    def __init__(self, sistemas, *, fabrica_python, fabrica_dir, spawn=None,
                 lock_dir=None, pid_vivo=None):
        self._meta = {s.slug: s for s in sistemas}
        self._fabrica_python = fabrica_python
        self._fabrica_dir = fabrica_dir
        self._spawn = spawn or _spawn_popen
        self._lock_dir = lock_dir or _LOCK_DIR_PADRAO
        self._pid_vivo = pid_vivo or _pid_vivo
        self._procs = {}                       # slug -> handle
        self._obitos = {}                      # slug -> fonte de óbito
        os.makedirs(self._lock_dir, exist_ok=True)

    # --- caminhos por slug ---------------------------------------------------
    def _lock_path(self, slug) -> str:
        return os.path.join(self._lock_dir, str(slug) + ".lock")

    def stderr_path(self, slug) -> str:
        return os.path.join(self._lock_dir, str(slug) + ".stderr")

    def _entrada_path(self, slug) -> str:
        return os.path.join(self._lock_dir, str(slug) + ".entrada.json")

    def _calibrados_path(self, slug) -> str:
        return os.path.join(self._lock_dir, str(slug) + ".calibrados.json")

    def _ler_lock(self, slug):
        path = self._lock_path(slug)
        try:
            with open(path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return None
        except (ValueError, OSError):
            self._remover_lock(path)
            return None
        pid = data.get("pid")
        if pid is None:
            return data                        # lock de INTENÇÃO (fail-closed: ocupado)
        if not self._pid_vivo(pid):
            self._remover_lock(path)           # PID morto → lock obsoleto → livre
            return None
        return data

    def _escrever_lock(self, slug, pid, raiz):
        tmp = self._lock_path(slug) + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"conta": str(slug), "course_url": str(raiz), "pid": pid,
                       "run_iniciado_ts": time.time()}, f)
        os.replace(tmp, self._lock_path(slug))

    def _remover_lock(self, path):
        try:
            os.remove(path)
        except OSError:
            pass

    def _materializar(self, slug, spec):
        """Grava entrada_inicial/calibrados em JSON (o entrypoint os lê por path)."""
        ep, cp = self._entrada_path(slug), self._calibrados_path(slug)
        with open(ep, "w", encoding="utf-8") as f:
            json.dump(dict(spec.entrada_inicial or {}), f, ensure_ascii=False)
        with open(cp, "w", encoding="utf-8") as f:
            json.dump(dict(spec.calibrados or {}), f, ensure_ascii=False)
        return ep, cp

    def _reap(self):
        """Colhe runs que ESTA encarnação spawnou e encerraram; grava o óbito
        (exit_code REAL + path do stderr) e remove o lock do slug."""
        for slug in [s for s, p in self._procs.items() if p.poll() is not None]:
            proc = self._procs.pop(slug)
            meta = self._meta.get(slug)
            raiz = meta.raiz if meta is not None else ""
            self._obitos[str(slug)] = {
                "conta": str(slug), "curso": str(raiz),
                "exit_code": getattr(proc, "returncode", None),
                "pid": getattr(proc, "pid", None),
                "stderr_path": self.stderr_path(slug)}
            path = self._lock_path(slug)
            try:
                with open(path) as f:
                    data = json.load(f)
            except (FileNotFoundError, ValueError, OSError):
                continue
            if data.get("conta") == str(slug):
                self._remover_lock(path)

    def drenar_obitos(self) -> dict:
        """{slug: fonte} dos runs que morreram desde a última drenagem (ZERA).
        Consumido por `vigia.autopsia` (o vigia é agnóstico à chave — passa a ser
        `slug` em vez de `conta`)."""
        self._reap()
        out = dict(self._obitos)
        self._obitos = {}
        return out

    def sistema_ativo(self, slug) -> bool:
        self._reap()
        lock = self._ler_lock(slug)
        return lock is not None

    # contrato-espelho: o vigia/loop consultam <alvo>_ativo; expõe também o nome
    # genérico `curso_ativo` para reuso direto do `_safe_ativo` do loop, se preciso.
    def curso_ativo(self, slug) -> bool:
        return self.sistema_ativo(slug)

    def disparar(self, slug):
        self._reap()
        meta = self._meta.get(slug)
        if meta is None:
            raise RuntimeError(f"sistema {slug!r} sem metadados (raiz) — não disparo")
        lock = self._ler_lock(slug)
        if lock is not None:
            return f"ja_rodando:{slug}"        # IDEMPOTENTE por slug (1 run por sistema)

        ep, cp = self._materializar(slug, meta)
        cmd = [self._fabrica_python, "-m", "sintetizador.rodar_sistema", str(meta.raiz),
               "--entrada", ep, "--calibrados", cp]
        env = dict(os.environ)
        env["PYTHONPATH"] = self._fabrica_dir
        stderr_path = self.stderr_path(slug)
        # Lock de INTENÇÃO (pid=None) ANTES do spawn — fecha o TOCTOU (um crash na
        # janela intenção→PID mantém o slug ocupado; nada re-dispara às cegas).
        self._escrever_lock(slug, None, meta.raiz)
        try:
            proc = self._spawn(cmd, env=env, cwd=self._fabrica_dir,
                               stderr_path=stderr_path)
        except Exception:
            self._remover_lock(self._lock_path(slug))
            raise
        self._procs[slug] = proc
        self._escrever_lock(slug, getattr(proc, "pid", None), meta.raiz)
        return f"run_iniciado:{slug}:pid={getattr(proc, 'pid', None)}"

    def matar(self, slug, *, espera_s=2.0) -> bool:
        """Mata o run TRAVADO deste slug (heartbeat congelado). SEGURO: sistema NÃO
        tem sessão/anti-ban a proteger (≠ captura, que jamais mata). SIGTERM →
        espera → SIGKILL; colhe o handle local (se for nosso filho) e remove o lock.
        Idempotente: sem run vivo → False."""
        lock = self._ler_lock(slug)
        proc = self._procs.get(slug)
        pid = None
        if proc is not None and proc.poll() is None:
            pid = proc.pid
        elif lock is not None:
            pid = lock.get("pid")
        if not self._pid_vivo(pid):
            # nada vivo a matar (ou lock de intenção sem pid): só limpa o lock.
            self._remover_lock(self._lock_path(slug))
            self._procs.pop(slug, None)
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        fim = time.time() + max(espera_s, 0.0)
        while time.time() < fim and self._pid_vivo(pid):
            time.sleep(0.05)
        if self._pid_vivo(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        # colhe o handle se for nosso filho (evita zumbi) e grava o óbito.
        if proc is not None:
            try:
                proc.wait(timeout=espera_s)
            except Exception:
                pass
            self._procs.pop(slug, None)
            meta = self._meta.get(slug)
            self._obitos[str(slug)] = {
                "conta": str(slug),
                "curso": str(meta.raiz if meta is not None else ""),
                "exit_code": getattr(proc, "returncode", None),
                "pid": pid, "stderr_path": self.stderr_path(slug)}
        self._remover_lock(self._lock_path(slug))
        return True


def carregar_sistemas(path) -> list:
    """Lê `sistemas.yaml` (registro F4-b, análogo a `carregar_cursos`). Campos
    obrigatórios (`slug`, `raiz`) ausentes → LEVANTA (fail-closed). Aditivo: sem o
    arquivo o loop roda só a captura."""
    import yaml
    with open(path) as f:
        dados = yaml.safe_load(f) or []
    out = []
    for d in dados:
        cad = d.get("cadencia", "sob_demanda")
        out.append(SistemaSpec(
            slug=d["slug"], raiz=d["raiz"],
            estado=d.get("estado", "ativo"),
            cadencia=cad if isinstance(cad, str) else str(cad),
            entrada_inicial=dict(d.get("entrada_inicial", {}) or {}),
            calibrados=dict(d.get("calibrado_por_tipo", {}) or {}),
            teto_dia_usd=float(d.get("teto_dia_usd", 5.0)),
            precisa_24x7=bool(d.get("precisa_24x7", False))))
    return out
