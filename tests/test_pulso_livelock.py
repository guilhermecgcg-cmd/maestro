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
