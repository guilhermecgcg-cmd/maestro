"""GUARD DE MOTOR FORA DO DAEMON (incidente 15/09 09:49–10:00).

EVIDÊNCIA (só leitura): outra sessão rodou À MÃO
`python -m motor.greenn https://sierramkt.greenn.club/ --course 115070` das 09:49 às 09:53:50,
sem lock. O daemon (PID 97083) viu pendência e disparou o motor Greenn DELE na mesma conta:
`decisoes/2026-09-15.jsonl` tem "disparei captura de https://sierramkt.greenn.club/
(tentativa 5)" às 09:50:55 e "(tentativa 6)" às 09:57:06; o `ps` das 10:00 mostrou o PID
90027 (PPID 97083) `motor.greenn https://sierramkt.greenn.club/`. Dois motores na conta, e
uma aula virou audio_erro (um processo apagou o áudio que o outro transcrevia).

MECANISMO: `LocalExecutor.disparar` só consultava o lock durável (`_ler_lock`); o motor
manual não grava lock => conta "livre" => spawn.

DUBLÊ COM DENTES: `TabelaPs` modela a saída do `ps -axww -o pid=,ppid=,args=` — pid, ppid
e argv SEPARADOS, com o próprio daemon (os.getpid()) presente como na tabela real. Os filhos
do FakeSpawn entram com PPID = daemon e o argv EXATO que o daemon montou: é o que prova que
o filho do próprio daemon não conta como estranho.
"""
import os

import pytest

from maestro import athena_local, causa, disjuntor
from maestro.adaptadores import captura
from tests.test_evidencia_reap import _Alertas, _SpawnTee, _VigiaNoMundo, _Voz
from tests.test_executor_local import FakeSpawn

PY = "/opt/aula/.venv/bin/python"
PY_REAL = "/Users/guilhermerodrigues/teste/aula/.venv/bin/python"
GREENN = "https://sierramkt.greenn.club/"          # as DUAS entradas reais do YAML: mesma conta
GREENN_YT = "https://ytubeclass.greenn.club/"
GREENN_OUTRO = "https://outroclub.greenn.club/"
CAD_ALFA = "https://membros.alfaresearch.com.br/"
CAD_VIRAL = "https://cursos.codigoviral.com.br/"
MK_TRIADE = "https://comunidade-triade.memberkit.com.br/"
MK_LEO = "https://leo-macieira.memberkit.com.br/"
HOT_A = "https://hotmart.com/pt-br/club/x/products/111"
HOT_B = "https://hotmart.com/pt-br/club/y/products/222"

MANUAL_INCIDENTE = f"{PY_REAL} -m motor.greenn https://sierramkt.greenn.club/ --course 115070"
PID_MANUAL, PID_SHELL = 90001, 89990


class TabelaPs:
    """Dublê de `_listar_processos`: [(pid, ppid, args)], com o launchd e o daemon SEMPRE
    presentes (a tabela real sempre os traz). `filhos_de` = um FakeSpawn: cada motor VIVO
    que o executor subiu aparece como filho do daemon, com o argv exato do disparo."""
    def __init__(self, *linhas, filhos_de=None):
        self.linhas = list(linhas)
        self.filhos_de = filhos_de
        self.leituras = 0

    def __call__(self):
        self.leituras += 1
        daemon = os.getpid()
        tabela = [(1, 0, "/sbin/launchd"), (daemon, 1, f"{PY} -m maestro.athena_local")]
        if self.filhos_de is not None:
            for c in self.filhos_de.calls:
                if c["proc"].poll() is None:
                    tabela.append((c["proc"].pid, daemon, " ".join(c["cmd"])))
        return tabela + list(self.linhas)


_PLAT = {GREENN: "greenn", GREENN_YT: "greenn", GREENN_OUTRO: "greenn", CAD_ALFA: "cademi",
         CAD_VIRAL: "cademi", MK_TRIADE: "memberkit", MK_LEO: "memberkit",
         HOT_A: "hotmart", HOT_B: "hotmart"}


def _curso(url, conta=None, total=0):
    """Curso com a conta do TENANT (a regra do YAML real: um tenant, uma conta), salvo
    `conta` explícita. Cademí exige session_path do tenant (sessao_por_tenant)."""
    plat = _PLAT[url]
    host = captura._chave_host(url)
    return captura.CursoLocal(url=url, conta=conta or f"{plat}-{host.split('.')[0]}",
                              plataforma=plat, total_esperado=total,
                              session_path=f".{host}-session.json")


def _ex(tmp_path, cursos, tabela, spawn=None, lock_dir=None):
    spawn = spawn or FakeSpawn()
    ex = captura.LocalExecutor(
        cursos, motor_python=PY, motor_dir=str(tmp_path / "aula"), spawn=spawn,
        lock_dir=lock_dir or str(tmp_path / "locks"), motor_log_dir=str(tmp_path / "logs"),
        pid_vivo=spawn.mundo.vivo, pendencias_fn=lambda u, d: None, processos_fn=tabela)
    return ex, spawn


def _locks(tmp_path):
    d = tmp_path / "locks"
    return sorted(n for n in os.listdir(d) if n.endswith(".lock")) if d.exists() else []


# ==========================================================================
# 1) O INCIDENTE: motor greenn manual vivo, sem lock => o daemon NÃO dispara
# ==========================================================================
def test_incidente_15set_motor_greenn_manual_sem_lock_barra_o_disparo_na_mesma_conta(tmp_path):
    tabela = TabelaPs((PID_SHELL, 1, "/bin/zsh"), (PID_MANUAL, PID_SHELL, MANUAL_INCIDENTE))
    conta = "greenn-principal"
    ex, sp = _ex(tmp_path, [_curso(GREENN, conta), _curso(GREENN_YT, conta)], tabela)
    for url in (GREENN, GREENN_YT):                        # os DOIS clubs são a mesma conta
        with pytest.raises(captura.MotorForaDoDaemon) as e:
            ex.disparar(url)
        assert isinstance(e.value, captura.ContaOcupada)   # o caminho "aguarda a vez"
        assert f"PID {PID_MANUAL}" in e.value.motivo and "motor.greenn" in e.value.motivo
        assert e.value.assinatura == f"pids:{PID_MANUAL}"
    assert sp.calls == []                                  # DENTES: nenhum 2º motor na conta
    assert _locks(tmp_path) == []                          # nem lock de intenção largado


@pytest.mark.parametrize("argv", [
    f"{PY_REAL} -m motor.greenn https://sierramkt.greenn.club --course 115070",   # sem barra
    f"{PY_REAL} -m motor.greenn https://sierramkt.greenn.club/",
    "python3 -u -m motor.greenn https://SierraMKT.greenn.club/curso/115070?aula=3",
    "python3.14 -X utf8 -mmotor.greenn --course=115070 https://www.sierramkt.greenn.club/",
    "/Library/Frameworks/Python.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/"
    "Python -m motor.greenn https://sierramkt.greenn.club/",
    f"{PY_REAL} -m motor.greenn --course 115070",          # sem URL: tenant do env => todas
    f"{PY_REAL} -m motor.greenn --reseed",
    # URL que não é tenant do YAML (a API): o tenant ainda veio do env => todas as contas
    f"{PY_REAL} -m motor.greenn --course 115070 --api https://api.greenn.com.br/v1",
    # o ps devolve o argv SEM aspas: o shlex fundiria "a'b -m motor.greenn c'd" num token só
    f"{PY_REAL} -X a'b -m motor.greenn c'd https://sierramkt.greenn.club/",
    f"{PY_REAL} -m motor.zelador greenn https://sierramkt.greenn.club/ --conta greenn-principal",
])
def test_variantes_do_argv_do_motor_manual_tambem_barram(tmp_path, argv):
    ex, sp = _ex(tmp_path, [_curso(GREENN, "greenn-principal")],
                 TabelaPs((PID_MANUAL, PID_SHELL, argv)))
    with pytest.raises(captura.MotorForaDoDaemon):
        ex.disparar(GREENN)
    assert sp.calls == []


# ==========================================================================
# 2) TEXTO NÃO É PROCESSO: só python com `-m motor.<x>` em tokens casa
# ==========================================================================
@pytest.mark.parametrize("argv", [
    "grep motor.greenn https://sierramkt.greenn.club/",
    "/bin/zsh -c python -m motor.greenn https://sierramkt.greenn.club/ --course 115070",
    "caffeinate -is python -m motor.greenn https://sierramkt.greenn.club/",
    "/usr/bin/tail -f /tmp/motor.greenn.log https://sierramkt.greenn.club/",
    f"{PY_REAL} scripts/relatorio.py -m motor.greenn https://sierramkt.greenn.club/",
    f"{PY_REAL} -c pass -m motor.greenn https://sierramkt.greenn.club/",
    f"{PY_REAL} -m maestro.athena_local https://sierramkt.greenn.club/",
    f"{PY_REAL} -m motorista https://sierramkt.greenn.club/",
])
def test_texto_do_motor_no_argv_de_outro_processo_nao_barra(tmp_path, argv):
    ex, sp = _ex(tmp_path, [_curso(GREENN, "greenn-principal")],
                 TabelaPs((PID_MANUAL, 1, argv)))
    assert ex.disparar(GREENN).startswith("local_iniciada")
    assert len(sp.calls) == 1


def test_grep_com_o_texto_nao_barra_e_o_python_fora_da_arvore_do_daemon_barra(tmp_path):
    # O dente pedido: a MESMA tabela, primeiro só com o grep (dispara), depois com o python
    # do incidente vivo fora da árvore do daemon (barra o curso seguinte da mesma conta).
    grep = (PID_SHELL, 1, "grep motor.greenn https://sierramkt.greenn.club/")
    tabela = TabelaPs(grep)
    conta = "greenn-principal"
    ex, sp = _ex(tmp_path, [_curso(GREENN, conta), _curso(GREENN_YT, conta)], tabela)
    assert ex.disparar(GREENN).startswith("local_iniciada")
    sp.calls[0]["proc"].encerrar(0)                        # o motor do daemon terminou
    ex.drenar_obitos()                                     # ... e a autópsia do ciclo o colheu
    tabela.linhas.append((PID_MANUAL, 1, MANUAL_INCIDENTE))
    with pytest.raises(captura.MotorForaDoDaemon):
        ex.disparar(GREENN_YT)
    assert len(sp.calls) == 1


# ==========================================================================
# 3) CONTA DIFERENTE DA MESMA PLATAFORMA dispara (tenant = host)
# ==========================================================================
@pytest.mark.parametrize("modulo, ocupado, livre", [
    ("motor.cademi", CAD_ALFA, CAD_VIRAL),
    ("motor.memberkit", MK_TRIADE, MK_LEO),
    ("motor.greenn", GREENN, GREENN_OUTRO),
])
def test_conta_diferente_da_mesma_plataforma_dispara(tmp_path, modulo, ocupado, livre):
    tabela = TabelaPs((PID_MANUAL, PID_SHELL, f"{PY_REAL} -m {modulo} {ocupado}"))
    ex, sp = _ex(tmp_path, [_curso(ocupado), _curso(livre)], tabela)
    assert ex.disparar(livre).startswith("local_iniciada")
    with pytest.raises(captura.MotorForaDoDaemon):
        ex.disparar(ocupado)
    assert len(sp.calls) == 1


@pytest.mark.parametrize("argv", [
    f"{PY_REAL} -m motor.cli --retentar",                  # sem URL
    f"{PY_REAL} -m motor.cli https://consumer.hotmart.com/course/abc",
    f"{PY_REAL} -m motor.worker_residencial",              # worker da fila: Hotmart
])
def test_motor_hotmart_manual_ocupa_a_conta_hotmart_e_so_ela(tmp_path, argv):
    tabela = TabelaPs((PID_MANUAL, PID_SHELL, argv))
    ex, sp = _ex(tmp_path, [_curso(HOT_A, "hotmart-principal"), _curso(MK_TRIADE)], tabela)
    with pytest.raises(captura.MotorForaDoDaemon):
        ex.disparar(HOT_A)
    assert ex.disparar(MK_TRIADE).startswith("local_iniciada")
    assert len(sp.calls) == 1


def test_tenant_fora_do_yaml_ocupa_todas_as_contas_da_plataforma_e_so_dela(tmp_path):
    # Fail-closed DOCUMENTADO: um motor Memberkit num tenant que o YAML não conhece pode estar
    # no mesmo login; as contas Memberkit esperam ele acabar. As outras plataformas seguem.
    argv = f"{PY_REAL} -m motor.memberkit https://quarto-tenant.memberkit.com.br/"
    ex, sp = _ex(tmp_path, [_curso(MK_TRIADE), _curso(MK_LEO), _curso(GREENN)],
                 TabelaPs((PID_MANUAL, PID_SHELL, argv)))
    for url in (MK_TRIADE, MK_LEO):
        with pytest.raises(captura.MotorForaDoDaemon):
            ex.disparar(url)
    assert ex.disparar(GREENN).startswith("local_iniciada")
    assert len(sp.calls) == 1


# ==========================================================================
# 4) O FILHO DO PRÓPRIO DAEMON não conta como estranho (nem em dobro)
# ==========================================================================
def test_filho_do_proprio_daemon_nao_conta_como_motor_estranho(tmp_path):
    # Duas contas Hotmart: pelo perfil FIXO do spec todo `motor.cli` casa as duas. O filho
    # que o daemon subiu para a conta A já é contado pelo lock de A; contá-lo de novo pelo
    # ps travaria B e mudaria o "contas diferentes rodam em paralelo".
    sp = FakeSpawn()
    tabela = TabelaPs(filhos_de=sp)
    ex, _ = _ex(tmp_path, [_curso(HOT_A, "a"), _curso(HOT_B, "b")], tabela, spawn=sp)
    assert ex.disparar(HOT_A).startswith("local_iniciada")
    pid_a = sp.calls[0]["proc"].pid
    assert (pid_a, os.getpid(), " ".join(sp.calls[0]["cmd"])) in tabela()   # está na tabela
    assert ex.disparar(HOT_A).startswith("ja_capturando")  # a idempotência vem antes do ps
    assert ex.disparar(HOT_B).startswith("local_iniciada")
    assert len(sp.calls) == 2


def test_neto_do_filho_e_filho_de_encarnacao_anterior_tambem_nao_contam(tmp_path):
    sp = FakeSpawn()
    cursos = [_curso(HOT_A, "a"), _curso(HOT_B, "b")]
    ex1, _ = _ex(tmp_path, cursos, TabelaPs(filhos_de=sp), spawn=sp)
    ex1.disparar(HOT_A)
    pid_a, cmd_a = sp.calls[0]["proc"].pid, " ".join(sp.calls[0]["cmd"])
    # o daemon REINICIOU: o filho foi reparentado ao launchd e o executor novo só o conhece
    # pelo lock durável; o motor dele ainda abriu um subprocesso python -m motor.cli (neto).
    tabela = TabelaPs((pid_a, 1, cmd_a), (pid_a + 500, pid_a, f"{PY} -m motor.cli {HOT_A}"))
    sp2 = FakeSpawn(mundo=sp.mundo)
    ex2, _ = _ex(tmp_path, cursos, tabela, spawn=sp2)
    assert ex2.disparar(HOT_B).startswith("local_iniciada")
    assert len(sp2.calls) == 1


def test_filho_vivo_da_propria_conta_cujo_lock_sumiu_ainda_barra(tmp_path):
    # O lock da conta foi apagado por fora com o motor do daemon VIVO: o lock não o conta
    # mais, então o ps conta (não é "em dobro") — nunca um 2º motor na conta.
    sp = FakeSpawn()
    outro = MK_TRIADE + "curso-2"
    _PLAT[outro] = "memberkit"
    conta = "memberkit-comunidade-triade"
    ex, _ = _ex(tmp_path, [_curso(MK_TRIADE, conta), _curso(outro, conta)],
                TabelaPs(filhos_de=sp), spawn=sp)
    ex.disparar(MK_TRIADE)
    for nome in _locks(tmp_path):
        os.remove(tmp_path / "locks" / nome)
    with pytest.raises(captura.MotorForaDoDaemon):
        ex.disparar(outro)
    assert len(sp.calls) == 1


# ==========================================================================
# 5) PS ILEGÍVEL => não dispara (fail-closed), sem travar para sempre
# ==========================================================================
@pytest.mark.parametrize("quebra", ["levanta", "sem_o_proprio_daemon", "linha_torta"])
def test_ps_ilegivel_nao_dispara_e_volta_a_disparar_quando_o_ps_volta(tmp_path, quebra):
    quebrado = [True]

    def tabela():
        if quebrado[0]:
            if quebra == "levanta":
                raise OSError("ps: operation not permitted")
            if quebra == "sem_o_proprio_daemon":
                return [(1, 0, "/sbin/launchd")]
            return captura._parse_ps(f"    1     0 /sbin/launchd\nps: lixo {os.getpid()}\n")
        return [(os.getpid(), 1, "python -m maestro.athena_local")]

    ex, sp = _ex(tmp_path, [_curso(GREENN, "greenn-principal")], tabela)
    for _ in range(3):
        with pytest.raises(captura.MotorForaDoDaemon) as e:
            ex.disparar(GREENN)
        assert e.value.assinatura == "ps-ilegivel"
        assert "fail-closed" in e.value.motivo
    assert sp.calls == [] and _locks(tmp_path) == []
    quebrado[0] = False                                    # o ps voltou: nada ficou preso
    assert ex.disparar(GREENN).startswith("local_iniciada")


# ==========================================================================
# 6) A JANELA portão/passe -> spawn: 2ª leitura com a intenção em disco
# ==========================================================================
def test_motor_que_sobe_entre_o_portao_e_o_spawn_e_barrado_sem_tocar_o_perfil(tmp_path):
    leituras = []

    def tabela():
        leituras.append(1)
        base = [(os.getpid(), 1, "python -m maestro.athena_local")]
        return base if len(leituras) == 1 else base + [(PID_MANUAL, 1, MANUAL_INCIDENTE)]

    ex, sp = _ex(tmp_path, [_curso(GREENN, "greenn-principal")], tabela)
    perfil = tmp_path / "aula" / captura._perfil_de_conta("greenn-principal")
    perfil.mkdir(parents=True)
    (perfil / "SingletonLock").write_text("host-90001")
    with pytest.raises(captura.MotorForaDoDaemon):
        ex.disparar(GREENN)
    assert len(leituras) == 2                              # releu com a intenção em disco
    assert sp.calls == [] and _locks(tmp_path) == []       # a intenção foi solta
    assert (perfil / "SingletonLock").exists()             # o perfil não foi tocado


def test_zelo_tambem_nao_abre_navegador_na_conta_com_motor_manual(tmp_path):
    cursos = [_curso(GREENN, "greenn-principal")]
    ex, sp = _ex(tmp_path, cursos, TabelaPs((PID_MANUAL, 1, MANUAL_INCIDENTE)))
    with pytest.raises(captura.MotorForaDoDaemon):
        ex.disparar_zelo("greenn:greenn-principal", cursos[0])
    assert sp.calls == [] and _locks(tmp_path) == []
    livre, sp_livre = _ex(tmp_path, cursos, TabelaPs())
    assert livre.disparar_zelo("greenn:greenn-principal", cursos[0]).startswith("zelo_iniciado")


def test_sem_injecao_o_executor_consulta_o_leitor_do_modulo(tmp_path, monkeypatch):
    # O main() do daemon NÃO passa `processos_fn`: o default tem de ser o leitor REAL do
    # módulo (`_listar_processos`), resolvido na hora — guard ligado sem ninguém lembrar.
    monkeypatch.setattr(captura, "_listar_processos",
                        TabelaPs((PID_MANUAL, 1, MANUAL_INCIDENTE)))
    sp = FakeSpawn()
    ex = captura.LocalExecutor(
        [_curso(GREENN, "greenn-principal")], motor_python=PY, motor_dir=str(tmp_path),
        spawn=sp, lock_dir=str(tmp_path / "locks"), pid_vivo=sp.mundo.vivo)
    with pytest.raises(captura.MotorForaDoDaemon):
        ex.disparar(GREENN)
    assert sp.calls == []


# ==========================================================================
# 7) NO LOOP: aguarda a vez, decisão registrada UMA vez, nada conta como falha
# ==========================================================================
class _Espinha:
    def __init__(self):
        self.regs = []

    def registrar_decisao(self, o_que, por_que, **kw):
        self.regs.append((o_que, por_que, kw))


def test_no_loop_aguarda_a_vez_com_decisao_registrada_e_sem_contar_falha(tmp_path):
    cursos = [captura.CursoLocal(GREENN, "greenn-principal", "greenn", total_esperado=18)]
    sp = _SpawnTee([""] * 3)
    manual = [(PID_MANUAL, 1, MANUAL_INCIDENTE)]
    ex = captura.LocalExecutor(
        cursos, motor_python=PY, motor_dir=str(tmp_path / "aula"), spawn=sp,
        lock_dir=str(tmp_path / "locks"), motor_log_dir=str(tmp_path / "logs"),
        pid_vivo=sp.vivo, pendencias_fn=lambda u, d: None,
        processos_fn=lambda: [(os.getpid(), 1, "python -m maestro.athena_local")] + manual)
    alertas, voz, esp, estado, voo = _Alertas(), _Voz(), _Espinha(), {}, {}
    kw = dict(disjuntor=disjuntor, vigia=_VigiaNoMundo(sp.vivo), causa=causa,
              alertas=alertas, lock_dir=str(tmp_path / "locks"),
              autopsia_dir=str(tmp_path / "aut"), boot_ts=0.0, espinha=esp)
    for i in range(4):
        athena_local.ciclo_local(cursos, ex, lambda c: (0, 18), voz, voo, estado,
                                 agora=1000.0 + 120 * i, **kw)
    assert sp.calls == []                                  # DENTES: nenhum motor do daemon
    st = estado[GREENN]
    assert st.get("tentativas", 0) == 0 and st.get("disj_falhas", 0) == 0
    assert alertas.mortes == [] and voz.escaladas == []
    regs = [r for r in esp.regs if r[0].startswith("não disparei")]
    assert len(regs) == 1                                  # uma vez por episódio, não por ciclo
    assert f"PID {PID_MANUAL}" in regs[0][0]
    assert regs[0][2]["origem"] == "athena-local/anti-ban"
    assert regs[0][2].get("escalada") is not True
    manual.clear()                                         # o motor manual terminou
    athena_local.ciclo_local(cursos, ex, lambda c: (0, 18), voz, voo, estado,
                             agora=1000.0 + 120 * 4, **kw)
    assert len(sp.calls) == 1 and estado[GREENN]["tentativas"] == 1


# ==========================================================================
# 9) REVISÃO DO 45b9134: submódulo do CLI, `--conta`, `--opcao=URL`, zelo na
#    2ª leitura, re-arme do aviso, pré-filtro antes do shlex, depuradores
# ==========================================================================
ED = "https://luanacarolina.entregadigital.app.br/"
_PLAT[ED] = "entregadigital"


@pytest.mark.parametrize("resto, barrada", [
    ("motor.greenn.cli --course 115070", GREENN),
    ("motor.memberkit.cli", MK_TRIADE),
    ("motor.cademi.__main__ --reseed", CAD_ALFA),
    ("motor.entregadigital.cli https://luanacarolina.appmagic.link", ED),
    ("motor.greenn.__main__ https://sierramkt.greenn.club/", GREENN),
])
def test_submodulo_do_cli_da_plataforma_tambem_barra(tmp_path, resto, barrada):
    # FALSO NEGATIVO achado na revisão: a regra comparava `s.modulo == modulo` exato, e
    # `python -m motor.greenn.cli` (o mesmo CLI, chamado pelo submódulo) passava calado.
    cursos = [_curso(GREENN), _curso(MK_TRIADE), _curso(CAD_ALFA), _curso(ED)]
    ex, sp = _ex(tmp_path, cursos, TabelaPs((PID_MANUAL, PID_SHELL, f"{PY_REAL} -m {resto}")))
    with pytest.raises(captura.MotorForaDoDaemon):
        ex.disparar(barrada)
    assert sp.calls == []


@pytest.mark.parametrize("resto, ocupado, livre", [
    ("motor.cademi.cli https://membros.alfaresearch.com.br/", CAD_ALFA, CAD_VIRAL),
    ("motor.memberkit.__main__ https://comunidade-triade.memberkit.com.br/", MK_TRIADE, MK_LEO),
])
def test_submodulo_com_tenant_no_argv_nao_trava_o_outro_tenant(tmp_path, resto, ocupado, livre):
    ex, sp = _ex(tmp_path, [_curso(ocupado), _curso(livre)],
                 TabelaPs((PID_MANUAL, PID_SHELL, f"{PY_REAL} -m {resto}")))
    assert ex.disparar(livre).startswith("local_iniciada")
    with pytest.raises(captura.MotorForaDoDaemon):
        ex.disparar(ocupado)
    assert len(sp.calls) == 1


@pytest.mark.parametrize("argv", [
    f"{PY_REAL} -m motor.zelador greenn --conta greenn-sierramkt",
    f"{PY_REAL} -m motor.zelador greenn --conta=greenn-sierramkt",
])
def test_conta_explicita_no_argv_ocupa_so_aquela_conta(tmp_path, argv):
    # Sem URL de tenant e num módulo que não é de plataforma: só o `--conta` diz de quem é.
    ex, sp = _ex(tmp_path, [_curso(GREENN), _curso(GREENN_OUTRO)],
                 TabelaPs((PID_MANUAL, PID_SHELL, argv)))
    with pytest.raises(captura.MotorForaDoDaemon):
        ex.disparar(GREENN)
    assert ex.disparar(GREENN_OUTRO).startswith("local_iniciada")
    assert len(sp.calls) == 1


def test_url_na_forma_opcao_igual_identifica_o_tenant(tmp_path):
    # `--home-url=https://...`: sem ler o valor depois do `=`, o argv parece "sem tenant" e a
    # regra 5 travaria TODAS as contas Greenn. Lido, só a do sierramkt espera.
    argv = f"{PY_REAL} -m motor.greenn --home-url=https://sierramkt.greenn.club/ --course 115070"
    ex, sp = _ex(tmp_path, [_curso(GREENN), _curso(GREENN_OUTRO)],
                 TabelaPs((PID_MANUAL, PID_SHELL, argv)))
    assert ex.disparar(GREENN_OUTRO).startswith("local_iniciada")
    with pytest.raises(captura.MotorForaDoDaemon):
        ex.disparar(GREENN)
    assert len(sp.calls) == 1


def test_zelo_rele_o_ps_com_os_locks_em_disco_antes_de_abrir_o_navegador(tmp_path):
    leituras = []

    def tabela():
        leituras.append(1)
        base = [(os.getpid(), 1, "python -m maestro.athena_local")]
        return base if len(leituras) == 1 else base + [(PID_MANUAL, 1, MANUAL_INCIDENTE)]

    cursos = [_curso(GREENN, "greenn-principal")]
    ex, sp = _ex(tmp_path, cursos, tabela)
    perfil = tmp_path / "aula" / captura._perfil_de_conta("greenn-principal")
    perfil.mkdir(parents=True)
    (perfil / "SingletonLock").write_text("host-90001")
    with pytest.raises(captura.MotorForaDoDaemon):
        ex.disparar_zelo("greenn:greenn-principal", cursos[0])
    assert len(leituras) == 2                              # releu com os locks nas mãos
    assert sp.calls == [] and _locks(tmp_path) == []       # os locks do zelo foram soltos
    assert (perfil / "SingletonLock").exists()             # o perfil não foi tocado


def test_no_loop_o_aviso_rearma_depois_de_um_disparo(tmp_path):
    # Episódio 1 (ps ilegível) -> decisão; o ps volta -> dispara; o motor termina produzindo;
    # episódio 2 com a MESMA assinatura (ps ilegível de novo) -> tem de registrar DE NOVO.
    cursos = [captura.CursoLocal(GREENN, "greenn-principal", "greenn", total_esperado=18)]
    sp = _SpawnTee([""] * 3)
    ps_ok, prog = [False], [0]

    def tabela():
        if not ps_ok[0]:
            raise OSError("ps: operation not permitted")
        return [(os.getpid(), 1, "python -m maestro.athena_local")]

    ex = captura.LocalExecutor(
        cursos, motor_python=PY, motor_dir=str(tmp_path / "aula"), spawn=sp,
        lock_dir=str(tmp_path / "locks"), motor_log_dir=str(tmp_path / "logs"),
        pid_vivo=sp.vivo, pendencias_fn=lambda u, d: None, processos_fn=tabela)
    alertas, voz, esp, estado, voo = _Alertas(), _Voz(), _Espinha(), {}, {}
    kw = dict(disjuntor=disjuntor, vigia=_VigiaNoMundo(sp.vivo), causa=causa,
              alertas=alertas, lock_dir=str(tmp_path / "locks"),
              autopsia_dir=str(tmp_path / "aut"), boot_ts=0.0, espinha=esp)

    def ciclo(agora):
        athena_local.ciclo_local(cursos, ex, lambda c: (prog[0], 18), voz, voo, estado,
                                 agora=agora, **kw)

    def avisos():
        return [r for r in esp.regs if r[0].startswith("não disparei")]

    ciclo(1000.0)
    assert len(avisos()) == 1
    ps_ok[0] = True
    ciclo(1120.0)
    assert len(sp.calls) == 1                              # disparou: a conta ficou livre
    prog[0] = 5                                            # o run produziu ...
    sp.calls[0]["proc"].encerrar(0)                        # ... e saiu limpo
    ps_ok[0] = False                                       # novo episódio, mesma assinatura
    ciclo(1240.0)
    ciclo(1360.0)
    assert len(sp.calls) == 1
    assert len(avisos()) == 2                              # DENTES: o aviso re-armou


def test_so_os_candidatos_a_motor_passam_pelo_shlex(tmp_path, monkeypatch):
    # Custo: a checagem roda 2x por disparo sobre ~500 processos. Só a linha cujo 1º token é
    # um python E que cita `motor` é tokenizada — as demais saem antes do shlex.
    chamadas = []
    tokens_real = captura._tokens
    monkeypatch.setattr(captura, "_tokens", lambda a: chamadas.append(a) or tokens_real(a))
    ruido = [(10000 + i, 1, f"/Applications/App {i}.app/Contents/MacOS/App --type=renderer "
                             f"--field-trial-handle={i}") for i in range(400)]
    ruido += [(20000 + i, 1, f"{PY_REAL} -m maestro.worker_{i} https://sierramkt.greenn.club/")
              for i in range(50)]
    ruido += [(30000 + i, 1, f"grep -r motor.greenn https://sierramkt.greenn.club/ {i}")
              for i in range(50)]
    ex, sp = _ex(tmp_path, [_curso(GREENN, "greenn-principal")],
                 TabelaPs(*ruido, (PID_MANUAL, 1, MANUAL_INCIDENTE)))
    with pytest.raises(captura.MotorForaDoDaemon):
        ex.disparar(GREENN)
    assert chamadas == [MANUAL_INCIDENTE]                  # 1 candidato, 1 tokenização


@pytest.mark.parametrize("argv", [
    f"{PY_REAL} -m pdb -m motor.greenn https://sierramkt.greenn.club/",
    f"{PY_REAL} -m cProfile -o /tmp/perfil.out -s cumtime -m motor.greenn https://sierramkt.greenn.club/",
    f"{PY_REAL} -m coverage run -m motor.greenn.cli --course 115070",
    f"{PY_REAL} -mtrace --count --module=motor.greenn https://sierramkt.greenn.club/",
    f"{PY_REAL} -m trace --count -C /tmp/cov --module motor.greenn https://sierramkt.greenn.club/",
    f"{PY_REAL} -m memray run -o /tmp/m.bin -m motor.greenn https://sierramkt.greenn.club/",
    f"{PY_REAL} -m debugpy --listen 5678 --wait-for-client -m motor.greenn https://sierramkt.greenn.club/",
    f"{PY_REAL} -m pdb -c continue -m motor.greenn https://sierramkt.greenn.club/",
    "/usr/local/bin/python3.12-intel64 -m motor.greenn https://sierramkt.greenn.club/",
    "/opt/homebrew/bin/python3.13t -m motor.greenn https://sierramkt.greenn.club/",
])
def test_motor_sob_depurador_ou_interpretador_com_sufixo_barra(tmp_path, argv):
    ex, sp = _ex(tmp_path, [_curso(GREENN, "greenn-principal")],
                 TabelaPs((PID_MANUAL, PID_SHELL, argv)))
    with pytest.raises(captura.MotorForaDoDaemon):
        ex.disparar(GREENN)
    assert sp.calls == []


@pytest.mark.parametrize("resto", [
    # FALSOS POSITIVOS da revisão do fcaf5c3: nenhum destes roda motor, e cada um travava a conta
    "-m coverage run -m pytest -m motor.greenn",                   # marcador do pytest
    "-m coverage run meu_script.py -m motor.greenn",               # script do coverage
    "-m cProfile -m maestro.relatorio -m motor.greenn https://sierramkt.greenn.club/",
    "-m pdb meu_script.py -m motor.greenn https://sierramkt.greenn.club/",
    "-m pdb -c continue meu_script.py -m motor.greenn https://sierramkt.greenn.club/",
    "-m coverage report -m motor.greenn",                          # `report -m` = linhas faltando
    "-m coverage -m motor.greenn https://sierramkt.greenn.club/",  # sem `run`: coverage só erra
])
def test_depois_do_depurador_so_o_primeiro_m_decide(tmp_path, resto):
    ex, sp = _ex(tmp_path, [_curso(GREENN), _curso(GREENN_OUTRO)],
                 TabelaPs((PID_MANUAL, PID_SHELL, f"{PY_REAL} {resto}")))
    assert ex.disparar(GREENN).startswith("local_iniciada")
    assert ex.disparar(GREENN_OUTRO).startswith("local_iniciada")
    assert len(sp.calls) == 2


def test_segundo_m_so_vale_depois_de_depurador(tmp_path):
    # `-m motor.x` depois de um módulo QUALQUER é argumento dele, não o que o python roda.
    argv = f"{PY_REAL} -m maestro.relatorio -m motor.greenn https://sierramkt.greenn.club/"
    ex, sp = _ex(tmp_path, [_curso(GREENN, "greenn-principal")],
                 TabelaPs((PID_MANUAL, PID_SHELL, argv)))
    assert ex.disparar(GREENN).startswith("local_iniciada")


# ==========================================================================
# 8) FUMAÇA do `ps` REAL (só leitura): a tabela inteira, com o próprio processo
# ==========================================================================
def test_fumaca_ps_real_traz_a_tabela_inteira_com_pid_ppid_e_argv(monkeypatch):
    monkeypatch.undo()                                     # desfaz o dublê do conftest
    tabela = captura._listar_processos()
    meu = [t for t in tabela if t[0] == os.getpid()]
    assert len(meu) == 1 and meu[0][1] == os.getppid()
    assert len(tabela) > 10
    assert "pytest" in meu[0][2]
    assert captura._modulo_motor(captura._tokens(meu[0][2])) is None   # pytest não é motor
