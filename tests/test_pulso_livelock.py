"""LIVELOCK do loop doméstico (branch fix-livelock-loop): o PULSO tem de provar PROGRESSO,
não só FIM DE CICLO.

EVIDÊNCIA (logs vivos em ~/.athena-local, conferidos): o vigia externo mata o loop quando o
pulso.json fica >900s sem ser regravado. Antes do fix o pulso só era gravado DEPOIS de um
`ciclo_local` COMPLETO — nada no boot, nada durante o ciclo. Em 07/08 11:32→11:52 (Mac
ACORDADO, leitura do Notion estourando o timeout de 120s) o vigia matou 4 loops
RECÉM-NASCIDOS seguidos (PIDs 17153, 17315, 17469, 17623), cada um preso no 1º curso do 1º
ciclo: nenhum chegou a gravar o 1º pulso, nenhuma captura saiu por 20+ min. O mesmo
"segundo kill" com o MESMO pulso velho se repete em ~20 episódios de 20/08 a 04/09.

DUBLÊS COM DENTES:
  - RELÓGIO FALSO (`athena_local.time`) + leitura LENTA do Notion (cada curso consome 150s
    simulados: o timeout de 120s do subprocesso + a escalada no Telegram) + um VIGIA
    SIMULADO com a regra EXATA do scripts/vigia_externo.sh em PRODUÇÃO (tick a cada 300s;
    mata se agora - ts > 900) que MATA a encarnação com uma BaseException (como o SIGTERM:
    os `except Exception` do ciclo não a engolem). O KeepAlive ressuscita (até 5x).
  - CHAMADA QUE NUNCA RETORNA (threading.Event): o pulso tem de APONTAR onde o loop parou
    e NÃO pode ser regravado por nenhum batimento independente — senão o vigia nunca
    pegaria um livelock REAL.
"""
import asyncio
import json
import os
import threading
import time
from types import SimpleNamespace

from maestro import athena_local
from maestro.adaptadores import captura

C1 = "https://hotmart.com/pt-br/x/products/111"
C2 = "https://hotmart.com/pt-br/y/products/222"
KIWIFY = "https://dooma.kiwify.com.br/curso-z"


class FakeVoz:
    def __init__(self):
        self.avisos = []
        self.escaladas = []

    def avisar_acao(self, acao):
        self.avisos.append(acao)

    def escalar(self, problema, pedido):
        self.escaladas.append((problema, pedido))


class FakeExecutor:
    """Contrato do LocalExecutor: idempotente por curso + ANTI-BAN 1-por-conta."""
    def __init__(self, conta_de):
        self._conta_de = conta_de
        self.ativos = set()
        self.disparos = []

    def curso_ativo(self, curso):
        return curso in self.ativos

    def disparar(self, curso):
        if curso in self.ativos:
            return f"ja:{curso}"
        conta = self._conta_de[curso]
        for c in self.ativos:
            if self._conta_de[c] == conta:
                raise captura.ContaOcupada(f"conta {conta} ocupada por {c}")
        self.ativos.add(curso)
        self.disparos.append(curso)
        return f"local_iniciada:{curso}"


def _curso(url, conta="a", plataforma="hotmart", total=18):
    return captura.CursoLocal(url=url, conta=conta, plataforma=plataforma,
                              total_esperado=total)


async def _noop_sleep(_):
    return None


def _ler(p):
    return json.loads(p.read_text())


# ==========================================================================
# Simulação do vigia externo EM PRODUÇÃO (a regra que matou os loops)
# ==========================================================================
class _Relogio:
    def __init__(self, t0):
        self.t = float(t0)

    def time(self):
        return self.t

    def avancar(self, s):
        self.t += s


class _VigiaMatou(BaseException):
    """O SIGTERM do vigia: a encarnação morre ONDE estiver (não é Exception, os
    `except Exception` do ciclo/passada/owner não podem engoli-lo)."""


class _VigiaSimulado:
    """Regra do scripts/vigia_externo.sh ANTES deste branch (a que rodou em produção):
    a cada tick (StartInterval=300) lê o pulso; presente e `agora - ts > 900` => KILL.
    (Em produção a idade usa max(mtime, ts); o daemon grava `ts=time.time()` no MESMO
    instante do write, então max(mtime, ts) == ts — o relógio falso controla os dois.)"""
    def __init__(self, pulso_path, relogio, *, tick_s=300.0, stall_s=900.0):
        self.path = pulso_path
        self.rel = relogio
        self.tick_s = tick_s
        self.stall_s = stall_s
        self.proximo = relogio.time() + tick_s
        self.idades = []
        self.mortes = 0

    def tick_se_devido(self):
        while self.rel.time() >= self.proximo:
            self.proximo += self.tick_s
            try:
                with open(self.path) as f:
                    ts = float(json.load(f)["ts"])
            except (OSError, ValueError, KeyError):
                continue                                   # sem pulso: NO_HEARTBEAT
            idade = self.rel.time() - ts
            self.idades.append(idade)
            if idade > self.stall_s:
                self.mortes += 1
                raise _VigiaMatou(f"LIVELOCK (pulso {idade:.0f}s)")


def test_ciclo_lento_com_notion_estourando_nao_vira_kill_loop(tmp_path, monkeypatch):
    """REPRODUZ 07/08 11:32→11:52. Sem o fix: toda encarnação é morta no 1º/2º curso do
    1º ciclo (o pulso ainda é o da encarnação anterior) -> 5 mortes, 0 ciclos: a captura
    TRAVA. Com o fix: pulso no boot + a cada curso -> nenhuma morte, o ciclo completa."""
    rel = _Relogio(1_788_000_000.0)
    monkeypatch.setattr(athena_local, "time", SimpleNamespace(time=rel.time))
    pulso = tmp_path / "pulso.json"
    # pulso órfão da encarnação ANTERIOR (a do 10/09 16:29: 545295s de idade)
    pulso.write_text(json.dumps({"ts": rel.time() - 545_295, "ciclo": 4, "ativos": []}))
    cursos = [_curso(f"https://hotmart.com/pt-br/c{n}/products/{n}", conta=f"k{n}")
              for n in range(12)]
    vigia = _VigiaSimulado(str(pulso), rel)
    tentados = []

    def notion_lento(curso):
        tentados.append(curso)
        rel.avancar(150.0)                                 # timeout 120s + escalada Telegram
        vigia.tick_se_devido()                             # o vigia roda enquanto isso
        raise RuntimeError("contagem LOCAL no Notion SEM confirmação (timed out)")

    ciclos = 0
    encarnacoes = 0
    for _ in range(5):                                     # KeepAlive ressuscita
        encarnacoes += 1
        try:
            ciclos += asyncio.run(athena_local.rodar(
                cursos, FakeExecutor({c.url: c.conta for c in cursos}), notion_lento,
                FakeVoz(), sleep=_noop_sleep, max_iters=1, intervalo_s=0.0,
                pulso_path=str(pulso)))
            break
        except _VigiaMatou:
            rel.avancar(30.0)                              # ThrottleInterval do launchd
    assert vigia.mortes == 0, (
        f"o vigia matou {vigia.mortes} encarnação(ões) de um loop que estava AVANÇANDO "
        f"(idades vistas: {[int(i) for i in vigia.idades]})")
    assert ciclos == 1 and encarnacoes == 1                # o ciclo COMPLETOU
    assert len(set(tentados)) == 12                        # e passou por TODOS os cursos
    assert max(vigia.idades) <= 900
    assert _ler(pulso)["ciclo"] == 1 and _ler(pulso)["fase"] == "fim"


def test_chamada_que_nunca_retorna_congela_o_pulso_apontando_o_ponto(tmp_path):
    """Um livelock REAL continua detectável: enquanto a leitura de C2 não retorna, o pulso
    (a) diz fase=passada/curso=C2/ciclo=1/pid do loop e (b) NÃO é regravado — o vigia vê
    a idade crescer e age. Sem o fix, não há pulso algum durante o ciclo."""
    pulso = tmp_path / "pulso.json"
    entrou, solta = threading.Event(), threading.Event()
    cursos = [_curso(C1, conta="a"), _curso(C2, conta="b")]

    def notion(curso):
        if curso == C2:
            entrou.set()
            solta.wait(30)                                 # "nunca retorna" (até soltar)
        return (0, 18)

    erros = []

    def roda():
        try:
            asyncio.run(athena_local.rodar(
                cursos, FakeExecutor({C1: "a", C2: "b"}), notion, FakeVoz(),
                sleep=_noop_sleep, max_iters=1, intervalo_s=0.0, pulso_path=str(pulso)))
        except BaseException as e:                         # pragma: no cover — diagnóstico
            erros.append(e)

    th = threading.Thread(target=roda, daemon=True)
    th.start()
    try:
        assert entrou.wait(10), "o ciclo nem chegou à chamada bloqueante"
        preso = _ler(pulso)
        assert preso["fase"] == "passada"
        assert preso["curso"] == C2
        assert preso["ciclo"] == 1
        assert preso["pid"] == os.getpid()
        mtime = pulso.stat().st_mtime_ns
        time.sleep(0.6)
        assert _ler(pulso) == preso                        # NADA regrava o pulso preso
        assert pulso.stat().st_mtime_ns == mtime
    finally:
        solta.set()
        th.join(10)
    assert not erros
    final = _ler(pulso)
    assert final["fase"] == "fim" and final["ciclo"] == 1
    assert set(final["ativos"]) == {C1, C2}                # contrato {ts,ciclo,ativos} mantido


def test_pulso_de_boot_substitui_o_orfao_da_encarnacao_anterior_antes_do_reaper(tmp_path):
    """10/09 16:29: o loop nascido no login (~20s de vida) foi morto por um pulso de 6 dias
    atrás. O 1º ato do loop tem de ser pulsar — ANTES do reaper e do 1º ciclo."""
    pulso = tmp_path / "pulso.json"
    pulso.write_text(json.dumps({"ts": time.time() - 545_295, "ciclo": 4, "ativos": []}))
    visto = []
    t_antes = time.time()
    asyncio.run(athena_local.rodar(
        [_curso(C1)], FakeExecutor({C1: "a"}), lambda c: (0, 18), FakeVoz(),
        sleep=_noop_sleep, max_iters=1, intervalo_s=0.0, pulso_path=str(pulso),
        reaper_fn=lambda: visto.append(_ler(pulso))))
    boot = visto[0]
    assert boot.get("fase") == "boot"
    assert boot["ciclo"] == 0
    assert boot["ts"] >= t_antes
    assert boot["pid"] == os.getpid()


# ==========================================================================
# O BOOT não pode apagar a EVIDÊNCIA da morte da encarnação anterior (achado da revisão)
# ==========================================================================
# Um lock de PID MORTO em ~/.athena-local/locks é a ÚNICA evidência de que a captura da
# encarnação anterior morreu (o loop novo não tem o Popen dela para drenar). Quem acha
# esse lock é a 1ª varredura de autópsia (vigia.autopsia(lock_dir), fase "autopsia" do 1º
# ciclo). O pulso de boot cheio e o reaper de boot rodam ANTES e chamavam
# LocalExecutor.curso_ativo -> _ler_lock, que APAGA lock de PID morto: a morte sumia sem
# autópsia. Dublês: o LocalExecutor REAL (lock_dir em tmp), o vigia e a causa REAIS, e um
# PID que o os.kill(pid, 0) real dá como inexistente.
_PID_MORTO = 99_999_999                        # > pid_max (macOS 99998) -> ProcessLookupError


class _SpawnEspiao:
    """Nenhuma captura real sobe no teste; registra se alguém tentou disparar."""
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, *, env, cwd):          # pragma: no cover — não deve ser chamado
        self.calls.append(cmd)
        raise AssertionError("o teste não dispara captura")


def _executor_local(cursos, tmp_path, spawn=None):
    return captura.LocalExecutor(
        cursos, motor_python="/nao/existe/python", motor_dir=str(tmp_path / "motor"),
        spawn=spawn or _SpawnEspiao(), lock_dir=str(tmp_path / "locks"),
        motor_log_dir=str(tmp_path / "logs"), pendencias_fn=lambda u, d: None)


def _autopsias_em(d):
    import glob
    return [json.loads(open(p).read()) for p in sorted(glob.glob(os.path.join(str(d), "*.json")))]


def _rodar_boot(cursos, executor, tmp_path, *, max_iters=1, reaper_fn=None):
    from maestro import causa, vigia
    return asyncio.run(athena_local.rodar(
        cursos, executor, lambda c: (18, 18), FakeVoz(), sleep=_noop_sleep,
        max_iters=max_iters, intervalo_s=0.0, pulso_path=str(tmp_path / "pulso.json"),
        vigia=vigia, causa=causa, lock_dir=str(tmp_path / "locks"),
        autopsia_dir=str(tmp_path / "aut"), reaper_fn=reaper_fn))


def test_lock_de_pid_morto_no_boot_ainda_gera_autopsia_no_1o_ciclo(tmp_path):
    cursos = [_curso(C1, conta="a")]
    # a encarnação ANTERIOR disparou C1 (lock durável com o PID) e a captura morreu
    # enquanto o loop estava fora do ar.
    _executor_local(cursos, tmp_path)._escrever_lock("a", C1, _PID_MORTO)
    novo = _executor_local(cursos, tmp_path)           # a encarnação que acabou de subir
    _rodar_boot(cursos, novo, tmp_path, max_iters=2)
    auts = _autopsias_em(tmp_path / "aut")
    # DENTES: com o pulso de boot CHEIO o lock era apagado antes da varredura -> 0 autópsias.
    assert len(auts) == 1, auts                        # e só UMA: o lock é limpo DEPOIS
    assert auts[0]["conta"] == "a" and auts[0]["curso"] == C1
    assert auts[0]["detectado_por"] == "pid"
    assert not os.listdir(tmp_path / "locks")           # limpo pelo pulso cheio de "fim"
    assert _ler(tmp_path / "pulso.json")["fase"] == "fim"


def test_reaper_de_boot_de_producao_nao_apaga_lock_de_pid_morto(tmp_path):
    # O reaper de PRODUÇÃO (`_reaper_de_boot`, o que o main() injeta) roda entre o pulso de
    # boot e o 1º ciclo e sondava a liveness com o curso_ativo DESTRUTIVO: mesma perda.
    from tests.test_reap_orphans_lane import _seed, _status, _tracker
    cursos = [_curso(C1, conta="a")]                    # C1 = .../products/111
    motor_dir = _tracker(tmp_path)
    _seed(motor_dir, [("h1", "transcrevendo", "pg-1", None)])   # órfão async da anterior
    _executor_local(cursos, tmp_path)._escrever_lock("a", C1, _PID_MORTO)
    novo = _executor_local(cursos, tmp_path)
    reaper = athena_local._reaper_de_boot(cursos, novo, lambda url: motor_dir)
    _rodar_boot(cursos, novo, tmp_path, reaper_fn=reaper)
    auts = _autopsias_em(tmp_path / "aut")
    assert len(auts) == 1 and auts[0]["detectado_por"] == "pid", auts   # DENTES
    # o gate anti-ban do reaper responde IGUAL (PID morto = sem captura viva -> reapa):
    assert _status(motor_dir)["h1"][0] == "pendente"


def test_curso_ativo_so_leitura_responde_igual_sem_apagar_o_lock(tmp_path):
    # Contrato do modo só-leitura: MESMA resposta do curso_ativo padrão em todos os casos
    # (vivo -> True; intenção pid=None -> True, fail-closed anti-ban; morto -> False);
    # a ÚNICA diferença é não apagar o lock obsoleto.
    cursos = [_curso(C1, conta="a"), _curso(C2, conta="b"),
              _curso("https://hotmart.com/pt-br/z/products/333", conta="c")]
    ex = captura.LocalExecutor(
        cursos, motor_python="/nao/existe/python", motor_dir=str(tmp_path / "motor"),
        spawn=_SpawnEspiao(), lock_dir=str(tmp_path / "locks"),
        motor_log_dir=str(tmp_path / "logs"), pid_vivo=lambda pid: pid == 4242)
    ex._escrever_lock("a", C1, 4242)                                   # vivo
    ex._escrever_lock("b", C2, None)                                   # intenção
    ex._escrever_lock("c", "https://hotmart.com/pt-br/z/products/333", _PID_MORTO)  # morto
    urls = [c.url for c in cursos]
    assert [ex.curso_ativo(u, limpar=False) for u in urls] == [True, True, False]
    assert len(os.listdir(tmp_path / "locks")) == 3                    # nada apagado
    assert [ex.curso_ativo(u) for u in urls] == [True, True, False]    # padrão: igual...
    assert len(os.listdir(tmp_path / "locks")) == 2                    # ...mas limpa o morto


def test_espera_longa_entre_ciclos_e_fatiada_e_regrava_o_pulso(tmp_path):
    """Um MAESTRO_INTERVALO_S > 900 faria o vigia matar o loop DURANTE o sono entre
    ciclos. A espera é fatiada (<=300s) e cada fatia regrava o pulso (fase=dormindo)."""
    pulso = tmp_path / "pulso.json"
    fatias = []

    async def sono(s):
        fatias.append((s, _ler(pulso)))

    asyncio.run(athena_local.rodar(
        [_curso(C1)], FakeExecutor({C1: "a"}), lambda c: (0, 18), FakeVoz(),
        sleep=sono, max_iters=1, intervalo_s=1000.0, pulso_path=str(pulso)))
    duracoes = [s for s, _ in fatias]
    assert abs(sum(duracoes) - 1000.0) < 1e-6              # dorme o MESMO total
    assert max(duracoes) <= 300.0
    fases = [p.get("fase") for _, p in fatias]
    assert fases[0] == "fim"
    assert fases[1:] and all(f == "dormindo" for f in fases[1:])


def test_intervalo_curto_segue_um_unico_sleep(tmp_path):
    """Regressão: no intervalo de produção (120s) o sono NÃO muda (uma chamada só)."""
    chamadas = []

    async def sono(s):
        chamadas.append(s)

    asyncio.run(athena_local.rodar(
        [_curso(C1)], FakeExecutor({C1: "a"}), lambda c: (0, 18), FakeVoz(),
        sleep=sono, max_iters=2, intervalo_s=120.0, pulso_path=str(tmp_path / "p.json")))
    assert chamadas == [120.0, 120.0]


def test_pulso_que_falha_nunca_derruba_o_loop():
    """Observabilidade é best-effort: um pulso ingravável (dir inexistente) não mata o
    loop nem impede a captura."""
    ex = FakeExecutor({C1: "a"})
    n = asyncio.run(athena_local.rodar(
        [_curso(C1)], ex, lambda c: (0, 18), FakeVoz(), sleep=_noop_sleep, max_iters=2,
        intervalo_s=0.0, pulso_path="/nao/existe/athena/pulso.json"))
    assert n == 2 and ex.disparos == [C1]


def test_ciclo_local_pulsa_em_cada_fase_e_em_cada_curso():
    """A ordem das batidas dentro de UM ciclo: início, gate (só plataforma nova, que
    escala no Telegram), autópsia e UMA batida antes de cada curso — é isso que mantém o
    pulso fresco num ciclo de 42 cursos com o Notion lento."""
    batidas = []
    cursos = [_curso(KIWIFY, conta="z", plataforma="kiwify"), _curso(C1, conta="a"),
              _curso(C2, conta="b")]
    athena_local.ciclo_local(
        cursos, FakeExecutor({C1: "a", C2: "b", KIWIFY: "z"}), lambda c: (0, 18),
        FakeVoz(), {}, {}, agora=1000.0, plataformas_suportadas=frozenset({"hotmart.com"}),
        pulsar=lambda fase, curso=None: batidas.append((fase, curso)))
    assert batidas == [("inicio", None), ("gate", KIWIFY), ("autopsia", None),
                       ("passada", C1), ("passada", C2)]


def test_pulsar_que_levanta_nao_derruba_o_ciclo():
    ex = FakeExecutor({C1: "a"})

    def pulsar_quebrado(fase, curso=None):
        raise OSError("disco cheio")

    athena_local.ciclo_local([_curso(C1)], ex, lambda c: (0, 18), FakeVoz(), {}, {},
                             agora=1000.0, pulsar=pulsar_quebrado)
    assert ex.disparos == [C1]


# ==========================================================================
# RUÍDO PÓS-REBOOT / PÓS-BOUNCE (item 1 da rodada 3). Com o boot preservando o lock de
# PID morto (item acima), CADA captura de uma encarnação anterior vira autópsia
# detectado_por=pid, exit_code None — e, sem o .err, com stderr VAZIO: alerta ESSENCIAL
# "causa desconhecida (fail-closed)" + backoff do disjuntor, um por conta que capturava
# (9 autópsias reais assim em ~/.athena-local/autopsias). Tudo REAL aqui: LocalExecutor
# (lock_dir e motor_log_dir em tmp), vigia, causa e o módulo disjuntor; o spawn é um
# dublê que TRUNCA o .err da conta como o `_spawn_popen` real ("wb" antes do Popen) —
# prova de que a autópsia lê a cauda ANTES do re-disparo apagá-la. O boot é injetado.
# ==========================================================================
from maestro import disjuntor as _disjuntor_real  # noqa: E402


class _SpawnQueTrunca:
    """Dublê do `_spawn_popen`: trunca o .err da conta (como o real) e devolve um
    processo 'vivo' (o PID do próprio pytest — os.kill(pid, 0) real diz vivo)."""
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, *, env, cwd):
        err = env.get(captura._ENV_STDERR_TEE)
        if err:
            os.makedirs(os.path.dirname(err), exist_ok=True)
            open(err, "wb").close()
        self.calls.append(cmd)
        return SimpleNamespace(pid=os.getpid(), returncode=None, poll=lambda: None)


class _AlertasEspiao:
    def __init__(self):
        self.mortes = []

    def captura_morreu(self, plataforma, motivo, **kw):
        self.mortes.append((motivo, kw))

    def sessao_expirada(self, plataforma, **kw):
        self.mortes.append(("SESSAO", kw))

    def curso_concluido(self, *a, **kw):
        pass

    def essenciais(self):
        return [m for m in self.mortes if m[1].get("essencial") or m[0] == "SESSAO"]


class _DisjuntorEspiao:
    """O módulo disjuntor REAL, com as falhas registradas contadas."""
    def __init__(self):
        self.falhas = []

    def pode_tentar(self, st, agora):
        return _disjuntor_real.pode_tentar(st, agora)

    def registrar_falha(self, st, agora):
        self.falhas.append(agora)
        _disjuntor_real.registrar_falha(st, agora)

    def registrar_sucesso(self, st):
        _disjuntor_real.registrar_sucesso(st)


def _orfa_da_encarnacao_anterior(tmp_path, cursos, conta, curso, *, err, quando):
    """A encarnação ANTERIOR disparou `curso` (lock durável com o PID) e o motor tee'ou
    `err` no .err da conta; tudo com mtime `quando`. Depois o processo sumiu."""
    ant = _executor_local(cursos, tmp_path)
    ant._escrever_lock(conta, curso, _PID_MORTO)
    os.utime(ant._lock_path(conta), (quando, quando))
    if err is not None:
        path = ant._stderr_path(conta)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(err)
        os.utime(path, (quando + 30, quando + 30))
    return ant._stderr_path(conta)


def _rodar_pos_boot(cursos, tmp_path, *, boot_ts, spawn):
    from maestro import causa, vigia
    alertas, disj = _AlertasEspiao(), _DisjuntorEspiao()
    novo = _executor_local(cursos, tmp_path, spawn=spawn)
    asyncio.run(athena_local.rodar(
        cursos, novo, lambda c: (0, 18), FakeVoz(), sleep=_noop_sleep, max_iters=1,
        intervalo_s=0.0, pulso_path=str(tmp_path / "pulso.json"), vigia=vigia, causa=causa,
        disjuntor=disj, alertas=alertas, lock_dir=str(tmp_path / "locks"),
        autopsia_dir=str(tmp_path / "aut"), boot_ts=boot_ts))
    [aut] = _autopsias_em(tmp_path / "aut")
    return aut, alertas, disj


_CAUDA_NO_MEIO_DO_RUN = (
    "2026-09-04 08:59:40,120 INFO motor.stoa.pipeline: aula 7/21 — baixando áudio\n"
    "2026-09-04 08:59:58,441 INFO httpx: HTTP Request: POST https://api.groq.com/openai/v1/"
    "audio/transcriptions \"HTTP/1.1 200 OK\"\n")


def test_reboot_captura_anterior_ao_boot_relanca_sem_alerta_e_sem_falha(tmp_path):
    # O Mac desligou no meio da captura (cauda parada no meio do run) e o loop subiu no
    # boot seguinte. DENTES (item 1b): antes -> "causa desconhecida (fail-closed)"
    # ESSENCIAL + registrar_falha no disjuntor. Agora: relancar SEM alerta e SEM falha,
    # e o curso é re-disparado no MESMO ciclo (never-stop).
    cursos = [_curso(C1, conta="a")]
    boot = time.time() - 600
    err = _orfa_da_encarnacao_anterior(tmp_path, cursos, "a", C1, err=_CAUDA_NO_MEIO_DO_RUN,
                                       quando=boot - 3600)
    spawn = _SpawnQueTrunca()
    aut, alertas, disj = _rodar_pos_boot(cursos, tmp_path, boot_ts=boot, spawn=spawn)
    assert aut["detectado_por"] == "pid" and aut["exit_code"] is None
    assert aut["lock_antes_do_boot"] is True
    assert aut["acao"] == "relancar" and aut["sem_falha"] is True, aut
    assert aut["motivo"].startswith("reinício/desligamento do Mac"), aut
    assert "aula 7/21" in aut["stderr_tail"]              # item 1a: a cauda do .err chegou
    assert alertas.essenciais() == [], alertas.mortes     # nenhum ping ao dono
    assert alertas.mortes == []                            # nem o só-log de "MORREU"/flap
    assert disj.falhas == []                               # sem backoff
    assert len(spawn.calls) == 1                           # re-disparado no mesmo ciclo
    assert open(err).read() == ""                          # ...que truncou o .err (depois)


def test_orfa_pos_bounce_que_saiu_limpa_nao_e_morte(tmp_path):
    # O caso REAL das 9 autópsias (bounces do vigia em 07/08, 19/08, 23/08 — nenhum
    # reboot nessas datas): a captura sobreviveu ao loop (start_new_session), terminou
    # LIMPA e o loop novo achou o lock com PID morto. DENTES: antes -> alerta ESSENCIAL
    # "MORREU ... causa desconhecida" + falha no disjuntor.
    from tests.test_causa import _CAUDA_LIMPA_STOA
    cursos = [_curso(C1, conta="a")]
    boot = time.time() - 30 * 86400                        # Mac de pé há 30 dias
    _orfa_da_encarnacao_anterior(tmp_path, cursos, "a", C1, err=_CAUDA_LIMPA_STOA,
                                 quando=time.time() - 7200)
    aut, alertas, disj = _rodar_pos_boot(cursos, tmp_path, boot_ts=boot,
                                         spawn=_SpawnQueTrunca())
    assert aut["lock_antes_do_boot"] is False
    assert aut["acao"] == "aguardar_backoff" and aut["sem_falha"] is True, aut
    assert "saída limpa de captura órfã" in aut["motivo"], aut
    assert alertas.mortes == [] and disj.falhas == []


def test_orfa_pos_bounce_que_crashou_e_classificada_pela_cauda_do_err(tmp_path):
    # DENTES (item 1a): uma órfã que MORREU de verdade (timeout real do page.goto) agora
    # é classificada pela cauda do .err (relancar, falha contada, sem alerta essencial)
    # em vez do cego "causa desconhecida" ESSENCIAL.
    from tests.test_causa import _STDERR_MEMBERKIT_TIMEOUT_GOTO
    cursos = [_curso(C1, conta="a")]
    _orfa_da_encarnacao_anterior(tmp_path, cursos, "a", C1,
                                 err=_STDERR_MEMBERKIT_TIMEOUT_GOTO,
                                 quando=time.time() - 7200)
    aut, alertas, disj = _rodar_pos_boot(cursos, tmp_path, boot_ts=time.time() - 86400,
                                         spawn=_SpawnQueTrunca())
    assert aut["acao"] == "relancar" and aut["sem_falha"] is False, aut
    assert "timeout" in aut["motivo"], aut
    assert alertas.essenciais() == [] and len(disj.falhas) == 1


def test_orfa_pos_boot_sem_cauda_segue_escalando_o_dono(tmp_path):
    # Contraprova (honestidade): sem .err e com o lock DEPOIS do boot, nada explica a
    # morte — segue fail-closed (alerta essencial + falha), como antes.
    cursos = [_curso(C1, conta="a")]
    _orfa_da_encarnacao_anterior(tmp_path, cursos, "a", C1, err=None,
                                 quando=time.time() - 7200)
    aut, alertas, disj = _rodar_pos_boot(cursos, tmp_path, boot_ts=time.time() - 86400,
                                         spawn=_SpawnQueTrunca())
    assert aut["acao"] == "escalar_humano" and aut["fonte"] == "fail-closed", aut
    assert len(alertas.essenciais()) == 1 and len(disj.falhas) == 1


def test_orfa_de_verdade_subprocesso_real_que_saiu_limpa_nao_alerta_nem_conta_falha(tmp_path):
    # PONTA A PONTA SEM DUBLÊ no caminho da evidência: a encarnação A dispara um motor
    # de verdade pelo `_spawn_popen` REAL (subprocesso python, tee REAL no .err da conta,
    # start_new_session) e morre sem drenar (o motor sai 0 imprimindo o resumo). A
    # encarnação B sobe e roda 1 ciclo com vigia/causa/disjuntor REAIS e o boot REAL do
    # Mac (sysctl, não injetado). Antes do item 1 (conferido rodando este cenário contra
    # o HEAD anterior): 'causa desconhecida (fail-closed)' ESSENCIAL + falha no disjuntor.
    import sys
    from maestro import causa, vigia
    motor = tmp_path / "motor"
    (motor / "motor").mkdir(parents=True)
    (motor / "motor" / "__init__.py").write_text("")
    (motor / "motor" / "cli.py").write_text(
        'print("2026-09-10 20:00:00,000 INFO motor.cli: sessão pronta (origem=state)")\n'
        'print("Stats: total=2 ok=2 audio=0 falhou=0")\n')
    cursos = [_curso(C1, conta="a")]
    kw = dict(motor_python=sys.executable, motor_dir=str(motor),
              lock_dir=str(tmp_path / "locks"), motor_log_dir=str(tmp_path / "logs"),
              pendencias_fn=lambda u, d: None)
    a = captura.LocalExecutor(cursos, **kw)                 # spawn = _spawn_popen REAL
    a.disparar(C1)
    a._procs[C1].wait(timeout=60)                           # o motor terminou; A 'morre'
    assert a._procs[C1].returncode == 0
    assert "Stats: total=2" in open(a._stderr_path("a")).read()    # o tee real gravou
    assert os.listdir(tmp_path / "locks")                   # e o lock ficou (A não drenou)
    alertas, disj = _AlertasEspiao(), _DisjuntorEspiao()
    b = captura.LocalExecutor(cursos, spawn=_SpawnEspiao(), **kw)
    asyncio.run(athena_local.rodar(
        cursos, b, lambda c: (18, 18), FakeVoz(), sleep=_noop_sleep, max_iters=1,
        intervalo_s=0.0, vigia=vigia, causa=causa, disjuntor=disj, alertas=alertas,
        lock_dir=str(tmp_path / "locks"), autopsia_dir=str(tmp_path / "aut")))
    [aut] = _autopsias_em(tmp_path / "aut")
    assert aut["detectado_por"] == "pid" and aut["exit_code"] is None
    assert aut["acao"] == "aguardar_backoff" and aut["sem_falha"] is True, aut
    assert "Stats: total=2 ok=2 audio=0 falhou=0" in aut["motivo"], aut
    assert alertas.mortes == [] and disj.falhas == []
