"""ZELADOR DE SESSÃO — o AGENDADOR do daemon (P7 etapa 2).

`maestro/zelador.py` decide QUANDO zelar cada conta ociosa e o que fazer com o resultado;
o `LocalExecutor` spawna `python -m motor.zelador <plat> <url> --conta <c>` segurando o
MESMO lock durável da conta (e das parceiras de arquivo/perfil); o vigia ignora locks de
reseed/zelador (M2); o `disparar` da captura cria o lock de intenção de forma EXCLUSIVA
(M3); morte provada vira UM alerta agregado com dedup persistido; o mtime da sessão depois
da morte dispara o zelo que, provando viva, rearma o latch de reseed (M4).

Dublês COM DENTES (mesmo padrão de tests/test_executor_local.py): `Mundo` é a tabela de
processos do SO (o `pid_vivo` do executor consulta ela, e ela atravessa encarnações);
`FakeSpawn` registra cmd/env/cwd e, ao terminar um zelo, escreve a linha `ZELADOR {json}`
no arquivo que o executor mandou o spawn tee'ar (é o que o `_spawn_popen` real faz com
stdout+stderr). O executor é o REAL, com lock_dir/logs em tmp. Nada navega, nada loga.
"""
import asyncio
import json
import os
import random

import pytest

from maestro import athena_local, vigia
from maestro import zelador as zmod
from maestro.adaptadores import captura

PY = "/opt/aula/.venv/bin/python"
T0 = 1_800_000_000.0
H = 3600.0
DIA = 24 * H

KIW = "https://dashboard.kiwify.com.br/courses"
KIW2 = "https://dashboard.kiwify.com.br/courses/outro"
MK_T = "https://comunidade-triade.memberkit.com.br/"
MK_E = "https://empreender.memberkit.com.br/"
STOA = "https://educacao.stoa.com.br/meus-cursos"
CUR = "https://membros.segueadi.com/"
HOT = "https://hotmart.com/pt-br/club/x/products/1"
HUB = "https://app.hub.la/user_groups/abc"
GR_Y = "https://ytubeclass.greenn.club/"
GR_S = "https://sierramkt.greenn.club/"


def _c(url, conta, plat, session_path=""):
    return captura.CursoLocal(url=url, conta=conta, plataforma=plat, session_path=session_path)


# --- dublês -------------------------------------------------------------------------------

class FakeProc:
    _seq = 7000

    def __init__(self):
        FakeProc._seq += 1
        self.pid = FakeProc._seq
        self.returncode = None

    def poll(self):
        return self.returncode

    def encerrar(self, code=0):
        self.returncode = code


class Mundo:
    def __init__(self):
        self.procs = {}

    def novo(self):
        p = FakeProc()
        self.procs[p.pid] = p
        return p

    def vivo(self, pid):
        p = self.procs.get(pid)
        return p is not None and p.poll() is None


class FakeSpawn:
    def __init__(self, mundo=None):
        self.mundo = mundo or Mundo()
        self.calls = []

    def __call__(self, cmd, *, env, cwd):
        proc = self.mundo.novo()
        self.calls.append({"cmd": list(cmd), "env": dict(env), "cwd": cwd, "proc": proc})
        return proc

    def zelos(self):
        return [c for c in self.calls if c["cmd"][2] == "motor.zelador"]

    def terminar(self, i, code, resultado=None, *, expira=None, detalhe="x", antes=""):
        """Encerra o i-ésimo spawn; se `resultado`, grava a linha ZELADOR no tee (como o
        motor/zelador.py real imprime no stdout, que o `_spawn_popen` tee'a)."""
        call = self.calls[i]
        path = call["env"].get("_ATHENA_MOTOR_STDERR")
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(antes)
                if resultado is not None:
                    linha = {"plataforma": call["cmd"][3], "conta": call["cmd"][6],
                             "resultado": resultado, "detalhe": detalhe, "expira_em": expira,
                             "vence_em_h": None, "provado_em": None}
                    f.write("ZELADOR " + json.dumps(linha) + "\n")
        call["proc"].encerrar(code)


class FakeAlertas:
    def __init__(self, entregue=True):
        self.logins = []
        self.vence = []
        self.entregue = entregue

    def logins_pendentes(self, alvos, comando):
        self.logins.append((tuple(alvos), comando))
        return self.entregue

    def sessao_vence(self, alvo, quando, dias, comando):
        self.vence.append((alvo, quando, dias, comando))
        return self.entregue

    def sessao_expirada(self, plataforma, **kw):
        raise AssertionError("o zelador não usa o alerta da captura")

    def captura_morreu(self, *a, **kw):
        return None

    def curso_concluido(self, *a, **kw):
        return None

    def maquina_sobrecarregada(self, *a, **kw):
        return None


class PortaoFake:
    def __init__(self, veto=None):
        self.veto = veto
        self.motores = []

    def avaliar(self, motores_ativos=0):
        self.motores.append(motores_ativos)
        return self.veto


def _exec(tmp, cursos, sp=None, **kw):
    sp = sp or FakeSpawn()
    kw.setdefault("comando_fn", lambda pid: None)
    ex = captura.LocalExecutor(
        cursos, motor_python=PY, motor_dir=str(tmp / "aula"), spawn=sp,
        lock_dir=str(tmp / "locks"), pid_vivo=sp.mundo.vivo, motor_log_dir=str(tmp / "logs"),
        motor_dir_por_plataforma={"stoa": str(tmp / "stoa")}, **kw)
    ex.sp = sp
    return ex


def _zel(tmp, cursos, *, modo="ligado", cfg=None, portao=None, seed=7):
    return zmod.Zelador(cursos, modo=modo, status_path=str(tmp / "sessoes-status.json"),
                        cfg=cfg or zmod.ConfigZelador(), portao=portao,
                        rng=random.Random(seed))


def _ocioso(ex, conta, desde):
    """A última captura da conta escreveu no .err dela em `desde` (o mtime do .err é o
    sinal de atividade que sobrevive a restart)."""
    p = ex._stderr_path(conta)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a"):
        pass
    os.utime(p, (desde, desde))


def _passo(z, ex, agora, *, estado=None, alertas=None):
    return z.passo(ex, {} if estado is None else estado, agora=agora,
                   alertas=alertas if alertas is not None else FakeAlertas())


def _lock(ex, conta):
    with open(ex._lock_path(conta)) as f:
        return json.load(f)


def _status(tmp):
    with open(tmp / "sessoes-status.json") as f:
        return json.load(f)


# ==========================================================================================
# DESLIGADO POR PADRÃO — só "1" liga; "seco" só escreve o status
# ==========================================================================================
def test_desligado_por_padrao_e_so_1_liga():
    assert zmod.modo_do_ambiente({}) == "desligado"
    assert zmod.modo_do_ambiente({"ATHENA_ZELADOR_ATIVO": "0"}) == "desligado"
    assert zmod.modo_do_ambiente({"ATHENA_ZELADOR_ATIVO": "sim"}) == "desligado"
    assert zmod.modo_do_ambiente({"ATHENA_ZELADOR_ATIVO": " seco "}) == "seco"
    assert zmod.modo_do_ambiente({"ATHENA_ZELADOR_ATIVO": "1"}) == "ligado"
    cursos = [_c(KIW, "kiwify-principal", "kiwify")]
    assert zmod.zelador_do_ambiente(cursos, env={}) is None
    assert zmod.zelador_do_ambiente(cursos, env={"ATHENA_ZELADOR_ATIVO": "0"}) is None


def test_zelador_do_ambiente_so_guarda_o_caminho_do_status(tmp_path):
    cursos = [_c(KIW, "kiwify-principal", "kiwify")]
    z = zmod.zelador_do_ambiente(cursos, env={"ATHENA_ZELADOR_ATIVO": "seco"})
    assert z.modo == "seco"
    assert z.status_path == os.path.join(os.path.expanduser("~"), ".athena-local",
                                         "sessoes-status.json")
    z = zmod.zelador_do_ambiente(cursos, env={"ATHENA_ZELADOR_ATIVO": "1",
                                              "ATHENA_SESSOES_STATUS": str(tmp_path / "s.json")})
    assert z.modo == "ligado" and z.status_path == str(tmp_path / "s.json")
    assert not (tmp_path / "s.json").exists()               # construir não escreve nada


def test_modo_seco_so_escreve_status_sem_disparar_nem_alertar(tmp_path):
    cursos = [_c(KIW, "kiwify-principal", "kiwify"), _c(CUR, "curseduca-segueadi", "curseduca")]
    ex, al = _exec(tmp_path, cursos), FakeAlertas()
    z = _zel(tmp_path, cursos, modo="seco")
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _ocioso(ex, "curseduca-segueadi", T0 - H)
    _passo(z, ex, T0, alertas=al)
    assert ex.sp.calls == [] and al.logins == [] and al.vence == []
    assert not os.listdir(tmp_path / "locks")
    st = _status(tmp_path)
    assert st["modo"] == "seco"
    assert {c["conta"]: c["decisao"] for c in st["contas"]} == {
        "kiwify-principal": "zelaria-agora", "curseduca-segueadi": "recente"}


# ==========================================================================================
# QUANDO zelar: conta ociosa há >= X h, intervalo sorteado 6–12 h
# ==========================================================================================
def test_zelar_so_conta_ociosa_ha_mais_de_X_horas(tmp_path):
    cursos = [_c(KIW, "kiwify-principal", "kiwify")]
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    _ocioso(ex, "kiwify-principal", T0 - 2 * H)              # a captura rodou há 2 h (já renova)
    _passo(z, ex, T0)
    assert ex.sp.calls == []
    _passo(z, ex, T0 + 4.5 * H)                               # ociosa há 6,5 h
    assert [c["cmd"] for c in ex.sp.calls] == [
        [PY, "-m", "motor.zelador", "kiwify", KIW, "--conta", "kiwify-principal"]]


def test_captura_viva_na_conta_bloqueia_o_zelo_e_conta_como_atividade(tmp_path):
    cursos = [_c(KIW, "kiwify-principal", "kiwify")]
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    ex.disparar(KIW)                                          # captura REAL pelo executor
    _passo(z, ex, T0)
    assert len(ex.sp.calls) == 1                              # só a captura
    ex.sp.calls[0]["proc"].encerrar(0)
    ex.drenar_obitos()
    _passo(z, ex, T0 + 5 * H)                                 # vista viva em T0: ociosa só há 5 h
    assert len(ex.sp.calls) == 1
    _passo(z, ex, T0 + 6.1 * H)
    assert len(ex.sp.calls) == 2 and ex.sp.calls[1]["cmd"][2] == "motor.zelador"


def test_intervalo_sorteado_6_12h_por_conta(tmp_path):
    cursos = [_c(KIW, "kiwify-principal", "kiwify"), _c(CUR, "curseduca-segueadi", "curseduca")]
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    for conta in ("kiwify-principal", "curseduca-segueadi"):
        _ocioso(ex, conta, T0 - DIA)
    _passo(z, ex, T0)
    ex.sp.terminar(0, 0, "viva")
    _passo(z, ex, T0 + 60)                                    # aplica o 1º e dispara o 2º
    ex.sp.terminar(1, 0, "viva")
    _passo(z, ex, T0 + 120)
    ivs = [st["_t"]["intervalo_s"] for st in z.estado.values()]
    assert all(6 * H <= iv <= 12 * H for iv in ivs)
    assert len(set(ivs)) == 2                                 # sorteado POR conta (sem relógio redondo)
    proximos = sorted(st["_t"]["proximo_ts"] for st in z.estado.values())
    _passo(z, ex, proximos[0] - 60)
    assert len(ex.sp.calls) == 2                              # nada antes do sorteado
    _passo(z, ex, proximos[0] + 1)
    assert len(ex.sp.calls) == 3


def test_intervalo_por_plataforma_calibravel_pelo_ambiente():
    cfg = zmod.ConfigZelador.do_ambiente({"ATHENA_ZELADOR_INTERVALO_H": "5-9",
                                          "ATHENA_ZELADOR_INTERVALO_H_GREENN": "3-4",
                                          "ATHENA_ZELADOR_OCIOSA_H": "2",
                                          "ATHENA_ZELADOR_PREVENTIVO_DIAS": "4",
                                          "ATHENA_ZELADOR_MAX_POR_HORA": "lixo"})
    assert cfg.intervalo("kiwify") == (5 * H, 9 * H)
    assert cfg.intervalo("greenn") == (3 * H, 4 * H)
    assert cfg.ociosa_s == 2 * H and cfg.preventivo_s == 4 * DIA
    assert cfg.max_por_hora == zmod.ConfigZelador().max_por_hora   # lixo => default


# ==========================================================================================
# LOCK: o MESMO lock durável da conta — nunca junto de captura
# ==========================================================================================
def test_zelar_segura_o_lock_e_nunca_roda_junto_da_captura_da_conta(tmp_path):
    cursos = [_c(KIW, "kiwify-principal", "kiwify")]
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0)
    zelo = ex.sp.calls[0]["proc"]
    lk = _lock(ex, "kiwify-principal")
    assert lk["dono"] == "zelador" and lk["pid"] == zelo.pid
    assert lk["course_url"] == "zelador:kiwify"
    with pytest.raises(captura.ContaOcupada):
        ex.disparar(KIW)
    assert len(ex.sp.calls) == 1
    assert ex.curso_ativo(KIW) is False                       # zelo NÃO é captura do curso
    ex.sp.terminar(0, 0, "viva")
    _passo(z, ex, T0 + 60)
    assert not os.path.exists(ex._lock_path("kiwify-principal"))
    ex.disparar(KIW)
    assert ex.sp.calls[1]["cmd"][2] == "motor.kiwify"


def test_zelar_segura_os_locks_das_contas_que_partilham_o_arquivo(tmp_path):
    sess = str(tmp_path / "aula" / ".memberkit-session.json")
    cursos = [_c(MK_T, "memberkit-triade", "memberkit", sess),
              _c(MK_E, "memberkit-empreender", "memberkit", sess)]
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    for conta in ("memberkit-triade", "memberkit-empreender"):
        _ocioso(ex, conta, T0 - DIA)
    ex.disparar(MK_E)                                         # a PARCEIRA captura
    _passo(z, ex, T0)
    assert [c["cmd"][2] for c in ex.sp.calls] == ["motor.memberkit"]   # nenhum zelo
    ex.sp.calls[0]["proc"].encerrar(0)
    ex.drenar_obitos()
    _passo(z, ex, T0 + 7 * H)
    zelo = ex.sp.calls[1]
    assert zelo["cmd"][2] == "motor.zelador" and zelo["cmd"][6] == "memberkit-triade"
    for conta in ("memberkit-triade", "memberkit-empreender"):
        lk = _lock(ex, conta)
        assert lk["dono"] == "zelador" and lk["pid"] == zelo["proc"].pid
    for url in (MK_T, MK_E):
        with pytest.raises(captura.ContaOcupada):
            ex.disparar(url)
    ex.sp.terminar(1, 0, "viva")
    _passo(z, ex, T0 + 7 * H + 60)            # colhe o da triade; a empreender também venceu
    zelo2 = ex.sp.calls[2]
    assert zelo2["cmd"][6] == "memberkit-empreender"
    for conta in ("memberkit-triade", "memberkit-empreender"):
        assert _lock(ex, conta)["pid"] == zelo2["proc"].pid   # de novo as DUAS travadas
    ex.sp.terminar(2, 0, "viva")
    _passo(z, ex, T0 + 7 * H + 120)
    assert not os.path.exists(ex._lock_path("memberkit-triade"))
    assert not os.path.exists(ex._lock_path("memberkit-empreender"))


def test_navegador_vivo_sem_lock_no_perfil_nao_abre_zelo(tmp_path):
    """Um `--reseed` avulso (que não segura o lock) com o Chrome aberto no perfil: o zelo
    não sobe por cima nem apaga o SingletonLock dele; e não sobra lock nenhum."""
    cursos = [_c(KIW, "kiwify-principal", "kiwify")]
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    perfil = tmp_path / "aula" / ".chrome-profile-kiwify-principal"
    perfil.mkdir(parents=True)
    chrome = ex.sp.mundo.novo()
    (perfil / "SingletonLock").symlink_to(f"host-{chrome.pid}")
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0)
    assert ex.sp.calls == []
    assert os.path.lexists(perfil / "SingletonLock")
    assert not os.path.exists(ex._lock_path("kiwify-principal"))


def test_navegador_orfao_depois_do_zelo_mantem_o_lock_com_o_pid_dele(tmp_path):
    cursos = [_c(KIW, "kiwify-principal", "kiwify")]
    perfil = tmp_path / "aula" / ".chrome-profile-kiwify-principal"
    perfil.mkdir(parents=True)
    sinais = []
    ex = _exec(tmp_path, cursos, sinal_fn=lambda pid, sig, grupo: sinais.append((pid, sig)))
    z = _zel(tmp_path, cursos)
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0)
    chrome = ex.sp.mundo.novo()                               # o Chrome que o zelo deixou
    (perfil / "SingletonLock").symlink_to(f"host-{chrome.pid}")
    ex.sp.terminar(0, 0, "viva")
    _passo(z, ex, T0 + 60)
    lk = _lock(ex, "kiwify-principal")
    assert lk["pid"] == chrome.pid and lk["dono"] == "zelador" and lk["navegador_orfao"]
    assert sinais == []                          # sem PROVA (comando ilegível) não mata ninguém
    with pytest.raises(captura.ContaOcupada):
        ex.disparar(KIW)
    chrome.encerrar(0)
    ex.disparar(KIW)                                          # órfão morto: a captura sobe
    assert ex.sp.calls[-1]["cmd"][2] == "motor.kiwify"


def test_navegador_orfao_com_prova_leva_sigterm(tmp_path):
    cursos = [_c(KIW, "kiwify-principal", "kiwify")]
    perfil = tmp_path / "aula" / ".chrome-profile-kiwify-principal"
    perfil.mkdir(parents=True)
    sinais = []
    ex = _exec(tmp_path, cursos, sinal_fn=lambda pid, sig, grupo: sinais.append((pid, sig, grupo)),
               comando_fn=lambda pid: f"/Applications/Chrome --user-data-dir={perfil} --x")
    z = _zel(tmp_path, cursos)
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0)
    chrome = ex.sp.mundo.novo()
    (perfil / "SingletonLock").symlink_to(f"host-{chrome.pid}")
    ex.sp.terminar(0, 0, "viva")
    _passo(z, ex, T0 + 60)
    import signal
    assert sinais == [(chrome.pid, signal.SIGTERM, False)]
    assert _lock(ex, "kiwify-principal")["pid"] == chrome.pid   # o lock só sai com ele morto


# ==========================================================================================
# 1 zelo por vez; teto por hora; adiado sob carga
# ==========================================================================================
def test_um_zelo_por_vez_nunca_paralelo(tmp_path):
    cursos = [_c(KIW, "kiwify-principal", "kiwify"), _c(CUR, "curseduca-segueadi", "curseduca"),
              _c(HUB, "hubla-principal", "hubla")]
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    for c in cursos:
        _ocioso(ex, c.conta, T0 - DIA)
    _passo(z, ex, T0)
    assert len(ex.sp.zelos()) == 1
    _passo(z, ex, T0 + 60)
    assert len(ex.sp.zelos()) == 1                            # o 1º ainda roda
    decisoes = sorted(c["decisao"] for c in _status(tmp_path)["contas"])
    assert decisoes == ["espera-outro-zelo", "espera-outro-zelo", "zelando"]  # nem tenta
    ex.sp.terminar(0, 0, "viva")
    _passo(z, ex, T0 + 120)
    assert len(ex.sp.zelos()) == 2
    # nova encarnação do daemon (restart) com o 2º zelo AINDA vivo: o lock em disco basta
    ex2 = _exec(tmp_path, cursos, sp=ex.sp)
    z2 = _zel(tmp_path, cursos)
    _passo(z2, ex2, T0 + 180)
    assert len(ex.sp.zelos()) == 2


def test_teto_de_zelos_por_hora(tmp_path):
    cursos = [_c(KIW, "kiwify-principal", "kiwify"), _c(CUR, "curseduca-segueadi", "curseduca"),
              _c(HUB, "hubla-principal", "hubla")]
    ex = _exec(tmp_path, cursos)
    z = _zel(tmp_path, cursos, cfg=zmod.ConfigZelador(max_por_hora=2))
    for c in cursos:
        _ocioso(ex, c.conta, T0 - DIA)
    _passo(z, ex, T0)
    ex.sp.terminar(0, 0, "viva")
    _passo(z, ex, T0 + 60)
    ex.sp.terminar(1, 0, "viva")
    _passo(z, ex, T0 + 120)
    assert len(ex.sp.zelos()) == 2                            # teto: 2 na última hora
    _passo(z, ex, T0 + H + 1)
    assert len(ex.sp.zelos()) == 3


def test_adia_sob_carga_e_so_tenta_de_novo_em_30_min(tmp_path):
    cursos = [_c(KIW, "kiwify-principal", "kiwify"), _c(HOT, "hotmart-principal", "hotmart")]
    portao = PortaoFake("máquina sobrecarregada (carga 7.0)")
    ex = _exec(tmp_path, cursos)
    z = _zel(tmp_path, cursos, portao=portao)
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    ex.disparar(HOT)                                          # 1 captura viva noutra conta
    _passo(z, ex, T0)
    assert ex.sp.zelos() == []
    assert portao.motores == [1]                              # o portão vê as capturas vivas
    assert {c["conta"]: c["decisao"] for c in _status(tmp_path)["contas"]}[
        "kiwify-principal"] == "adiada-carga"
    portao.veto = None
    _passo(z, ex, T0 + 10 * 60)
    assert ex.sp.zelos() == []                                # adiado: só em 30 min
    _passo(z, ex, T0 + 31 * 60)
    assert len(ex.sp.zelos()) == 1


def test_portao_do_zelo_e_mais_estrito_que_o_da_captura():
    from maestro import carga

    def sensor(c, m):
        return lambda: carga.LeituraCarga(carga_1min=c, nucleos=8, mem_livre_pct=m)

    assert zmod.portao_do_ambiente(env={}, sensor=sensor(7.0, 50.0)).avaliar(0)   # 7 > 0,75×8
    assert zmod.portao_do_ambiente(env={}, sensor=sensor(2.0, 20.0)).avaliar(0)   # mem < 25%
    p = zmod.portao_do_ambiente(env={}, sensor=sensor(2.0, 50.0))
    assert p.avaliar(1) is None                                # 1 captura viva: pode
    assert p.avaliar(2)                                        # 2: adia
    assert carga.PortaoCarga(sensor=sensor(7.0, 20.0)).avaliar(0) is None   # a captura deixaria
    p = zmod.portao_do_ambiente(env={"ATHENA_ZELADOR_CARGA_FATOR": "1",
                                     "ATHENA_ZELADOR_MEM_LIVRE_MIN_PCT": "15",
                                     "ATHENA_ZELADOR_MAX_CAPTURAS": "2"},
                                sensor=sensor(7.0, 20.0))
    assert p.avaliar(2) is None and p.avaliar(3)


# ==========================================================================================
# RESULTADOS: morte provada, prova fraca, inconclusivo
# ==========================================================================================
def test_morte_provada_para_de_zelar_ate_o_rearme(tmp_path):
    cursos = [_c(KIW, "kiwify-principal", "kiwify")]
    ex, z, al = _exec(tmp_path, cursos), _zel(tmp_path, cursos), FakeAlertas()
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0, alertas=al)
    ex.sp.terminar(0, 3, "morta", detalhe="SessionDeadError")
    _passo(z, ex, T0 + 60, alertas=al)
    st = z.estado["kiwify:kiwify-principal"]
    assert st["status"] == "aguardando-humano" and st["mortes_seguidas"] == 1
    assert len(al.logins) == 1
    for dt in (H, 13 * H, 2 * DIA, 30 * DIA):
        _passo(z, ex, T0 + dt, alertas=al)
    assert len(ex.sp.zelos()) == 1                            # zero martelamento deslogado
    assert len(al.logins) == 1                                # e zero alerta repetido


def test_morte_de_prova_fraca_exige_segunda_morte_espacada(tmp_path):
    cursos = [_c(STOA, "stoa-principal", "stoa")]
    ex, z, al = _exec(tmp_path, cursos), _zel(tmp_path, cursos), FakeAlertas()
    _ocioso(ex, "stoa-principal", T0 - DIA)
    _passo(z, ex, T0, alertas=al)
    ex.sp.terminar(0, 3, "morta")
    _passo(z, ex, T0 + 60, alertas=al)
    st = z.estado["stoa:stoa-principal"]
    assert st["status"] == "morte-suspeita" and al.logins == []
    _passo(z, ex, T0 + 20 * 60, alertas=al)
    assert len(ex.sp.zelos()) == 1                            # confirmação só >= 30 min depois
    _passo(z, ex, T0 + 31 * 60, alertas=al)
    assert len(ex.sp.zelos()) == 2
    ex.sp.terminar(1, 3, "morta")
    _passo(z, ex, T0 + 32 * 60, alertas=al)
    assert st["status"] == "aguardando-humano"
    assert [a for a, _ in al.logins] == [("stoa:stoa-principal",)]


def test_viva_entre_mortes_de_prova_fraca_zera_a_suspeita(tmp_path):
    cursos = [_c(HUB, "hubla-principal", "hubla")]
    ex, z, al = _exec(tmp_path, cursos), _zel(tmp_path, cursos), FakeAlertas()
    _ocioso(ex, "hubla-principal", T0 - DIA)
    _passo(z, ex, T0, alertas=al)
    ex.sp.terminar(0, 3, "morta")
    _passo(z, ex, T0 + 60, alertas=al)
    _passo(z, ex, T0 + 31 * 60, alertas=al)
    ex.sp.terminar(1, 0, "viva")
    _passo(z, ex, T0 + 32 * 60, alertas=al)
    st = z.estado["hubla:hubla-principal"]
    assert st["status"] == "viva" and st["mortes_seguidas"] == 0
    iv = st["_t"]["intervalo_s"]
    _passo(z, ex, T0 + 32 * 60 + iv + 1, alertas=al)
    ex.sp.terminar(2, 3, "morta")
    _passo(z, ex, T0 + 32 * 60 + iv + 60, alertas=al)
    assert st["status"] == "morte-suspeita" and al.logins == []   # recomeça do zero


def test_inconclusivo_nunca_vira_morte_nem_bencha_a_captura(tmp_path):
    cursos = [_c(KIW, "kiwify-principal", "kiwify")]
    estado = {KIW: {"fase": captura.FASE_NOVO}}
    ex, z, al = _exec(tmp_path, cursos), _zel(tmp_path, cursos), FakeAlertas()
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0, estado=estado, alertas=al)
    ex.sp.terminar(0, 5, "inconclusiva", detalhe="SessionProbeInconclusiveError")
    _passo(z, ex, T0 + 60, estado=estado, alertas=al)
    _passo(z, ex, T0 + 60 + H - 60, estado=estado, alertas=al)
    assert len(ex.sp.zelos()) == 1                            # backoff de 1 h
    _passo(z, ex, T0 + 60 + H, estado=estado, alertas=al)
    ex.sp.terminar(1, 5, "inconclusiva")
    _passo(z, ex, T0 + 2 * H, estado=estado, alertas=al)
    _passo(z, ex, T0 + 2 * H + 2 * H - 60, estado=estado, alertas=al)
    assert len(ex.sp.zelos()) == 2                            # backoff dobra: 2 h
    st = z.estado["kiwify:kiwify-principal"]
    assert st["status"] != "aguardando-humano" and st["inconclusivas_seguidas"] == 2
    assert al.logins == []
    assert estado == {KIW: {"fase": captura.FASE_NOVO}}      # captura intocada: sem bench/disjuntor
    assert ex.drenar_obitos() == {}                           # nenhum óbito de captura
    assert vigia.autopsia(ex._lock_dir, {}, pid_vivo=ex.sp.mundo.vivo, agora=T0 + 3 * H,
                          autopsia_dir=str(tmp_path / "aut")) == []


@pytest.mark.parametrize("code,resultado", [(3, None), (0, None), (1, None), (-9, None),
                                            (0, "morta"), (3, "viva")])
def test_saida_sem_a_linha_certa_nunca_e_prova(tmp_path, code, resultado):
    """Exit e linha têm de concordar: exit 3 sem a linha `morta` (tee falhou, módulo
    ausente na árvore, crash) é inconclusivo — nunca alerta de login à toa; exit 0 sem a
    linha `viva` nunca vira prova de vida."""
    cursos = [_c(KIW, "kiwify-principal", "kiwify")]
    ex, z, al = _exec(tmp_path, cursos), _zel(tmp_path, cursos), FakeAlertas()
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0, alertas=al)
    ex.sp.terminar(0, code, resultado)
    _passo(z, ex, T0 + 60, alertas=al)
    st = z.estado["kiwify:kiwify-principal"]
    assert st["resultado"] == "inconclusiva"
    assert st["status"] == "desconhecida" and st["_t"].get("provado_ts") is None
    assert al.logins == []


def test_zelo_de_encarnacao_anterior_que_morreu_nao_vira_obito_de_captura(tmp_path):
    cursos = [_c(KIW, "kiwify-principal", "kiwify")]
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0)
    ex2 = _exec(tmp_path, cursos, sp=ex.sp)                   # o daemon reiniciou no meio do zelo
    ex.sp.calls[0]["proc"].encerrar(3)                        # o zelo órfão terminou
    obitos = vigia.autopsia(ex2._lock_dir, ex2.drenar_obitos(), pid_vivo=ex.sp.mundo.vivo,
                            agora=T0 + 60, autopsia_dir=str(tmp_path / "aut"))
    assert obitos == [] and not (tmp_path / "aut").exists()
    assert ex2.conta_ocupada("kiwify-principal") is False     # lock de PID morto liberado


# ==========================================================================================
# M2 — o vigia ignora locks de reseed/zelador; M3 — intenção exclusiva no disparar
# ==========================================================================================
@pytest.mark.parametrize("dono", ["reseed", "zelador"])
def test_vigia_ignora_lock_de_reseed_e_zelador(tmp_path, dono):
    ld = tmp_path / "locks"
    ld.mkdir()
    (ld / "a.lock").write_text(json.dumps({"pid": 999_999, "course_url": f"{dono}:kiwify",
                                           "conta": "kiwify-principal", "ts": T0, "dono": dono}))
    obitos = vigia.autopsia(str(ld), {}, pid_vivo=lambda p: False, agora=T0,
                            autopsia_dir=str(tmp_path / "aut"), boot_ts=T0 - DIA)
    assert obitos == [] and not (tmp_path / "aut").exists()


def test_vigia_segue_autopsiando_lock_de_captura(tmp_path):
    ld = tmp_path / "locks"
    ld.mkdir()
    (ld / "a.lock").write_text(json.dumps({"pid": 999_999, "course_url": KIW,
                                           "conta": "kiwify-principal", "ts": T0}))
    obitos = vigia.autopsia(str(ld), {}, pid_vivo=lambda p: False, agora=T0,
                            autopsia_dir=str(tmp_path / "aut"), boot_ts=T0 - DIA)
    assert [o.conta for o in obitos] == ["kiwify-principal"]


def test_lock_de_intencao_atomico_contra_dono_externo(tmp_path):
    """A corrida real: o `disparar` lê o lock (livre), escolhe o passe (SQLite, até ~10 s) e
    só então grava a intenção. Se o reseed pegou o lock NESSA janela, sobrescrever (o
    `os.replace` antigo) subiria a captura por cima do login do humano e apagaria o
    SingletonLock do navegador dele. Criação EXCLUSIVA: o de fora vence, nada é aberto."""
    cursos = [_c(HOT, "hotmart-principal", "hotmart")]
    externo = {"pid": 4242, "course_url": "reseed:hotmart", "conta": "hotmart-principal",
               "ts": T0, "dono": "reseed"}
    perfil = tmp_path / "aula" / ".chrome-profile"
    perfil.mkdir(parents=True)
    (perfil / "SingletonLock").symlink_to("host-4242")
    box = {}

    def pendencias(url, motor_dir):                  # a janela do SQLite: o reseed entra AQUI
        with open(box["ex"]._lock_path("hotmart-principal"), "w") as f:
            json.dump(externo, f)
        return {"base": 1, "audio": 0, "embed": 0, "nao-video": 0, "youtube": 0}

    ex = _exec(tmp_path, cursos, pendencias_fn=pendencias)
    box["ex"] = ex
    with pytest.raises(captura.ContaOcupada):
        ex.disparar(HOT)
    assert _lock(ex, "hotmart-principal") == externo
    assert ex.sp.calls == []
    assert os.path.lexists(perfil / "SingletonLock")


# ==========================================================================================
# ALERTAS: agregado com o comando exato (dedup persistido), preventivo só relógio duro
# ==========================================================================================
def test_alerta_agregado_com_comando_exato_dedup_persistente(tmp_path):
    cursos = [_c(KIW, "kiwify-principal", "kiwify"), _c(CUR, "curseduca-segueadi", "curseduca")]
    ex, z, al = _exec(tmp_path, cursos), _zel(tmp_path, cursos), FakeAlertas()
    for c in cursos:
        _ocioso(ex, c.conta, T0 - DIA)
    _passo(z, ex, T0, alertas=al)
    ex.sp.terminar(0, 3, "morta")
    _passo(z, ex, T0 + 60, alertas=al)                        # aplica e dispara o 2º
    ex.sp.terminar(1, 3, "morta")
    _passo(z, ex, T0 + 120, alertas=al)
    assert al.logins[-1] == (
        ("curseduca:curseduca-segueadi", "kiwify:kiwify-principal"),
        "uv run scripts/reseed.py curseduca:curseduca-segueadi kiwify:kiwify-principal")
    n = len(al.logins)
    _passo(z, ex, T0 + 180, alertas=al)
    assert len(al.logins) == n                                # mesmo conjunto: silêncio
    ex2, z2 = _exec(tmp_path, cursos, sp=ex.sp), _zel(tmp_path, cursos)   # restart do daemon
    _passo(z2, ex2, T0 + 240, alertas=al)
    assert len(al.logins) == n                                # o dedup sobreviveu ao restart
    mortos = [c for c in _status(tmp_path)["contas"] if c["status"] == "aguardando-humano"]
    assert sorted((c["plataforma"], c["conta"]) for c in mortos) == [
        ("curseduca", "curseduca-segueadi"), ("kiwify", "kiwify-principal")]   # reseed tudo-morto


def test_alerta_que_nao_chegou_tenta_de_novo(tmp_path):
    cursos = [_c(KIW, "kiwify-principal", "kiwify")]
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    falho = FakeAlertas(entregue=False)
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0, alertas=falho)
    ex.sp.terminar(0, 3, "morta")
    _passo(z, ex, T0 + 60, alertas=falho)
    _passo(z, ex, T0 + 120, alertas=falho)
    assert len(falho.logins) == 2                             # não entregou: não arma o dedup


def test_greenn_dois_clubs_dois_zelos_em_serie_e_um_alvo_no_alerta(tmp_path):
    aula = tmp_path / "aula"
    cursos = [_c(GR_Y, "greenn-principal", "greenn", str(aula / ".greenn-session.json")),
              _c(GR_S, "greenn-principal", "greenn", str(aula / ".sierramkt-session.json"))]
    unidades = zmod.unidades_de(cursos)
    assert len(unidades) == 2 and {u.alvo for u in unidades} == {"greenn:greenn-principal"}
    ex, z, al = _exec(tmp_path, cursos), _zel(tmp_path, cursos), FakeAlertas()
    _ocioso(ex, "greenn-principal", T0 - DIA)
    _passo(z, ex, T0, alertas=al)
    ex.sp.terminar(0, 3, "morta")
    _passo(z, ex, T0 + 60, alertas=al)
    ex.sp.terminar(1, 3, "morta")
    _passo(z, ex, T0 + 120, alertas=al)
    z1, z2 = ex.sp.zelos()
    assert z1["env"]["GREENN_SESSION_PATH"] != z2["env"]["GREENN_SESSION_PATH"]
    assert {z1["cmd"][4], z2["cmd"][4]} == {GR_Y, GR_S}
    assert al.logins == [(("greenn:greenn-principal",),
                          "uv run scripts/reseed.py greenn:greenn-principal")]


def _cfg_rapida(**kw):
    return zmod.ConfigZelador(ociosa_s=H, intervalo_min_s=H, intervalo_max_s=H, **kw)


def _ciclo_viva(z, ex, al, agora, i, expira):
    _passo(z, ex, agora, alertas=al)
    assert len(ex.sp.zelos()) == i + 1, f"zelo {i} não disparou"
    ex.sp.terminar(len(ex.sp.calls) - 1, 0, "viva", expira=expira)
    _passo(z, ex, agora + 60, alertas=al)


def test_alerta_preventivo_so_relogio_duro_dedup_pelo_valor(tmp_path):
    cursos = [_c(CUR, "curseduca-segueadi", "curseduca")]
    ex, al = _exec(tmp_path, cursos), FakeAlertas()
    z = _zel(tmp_path, cursos, cfg=_cfg_rapida())
    _ocioso(ex, "curseduca-segueadi", T0 - DIA)
    E = T0 + 2 * DIA
    _ciclo_viva(z, ex, al, T0, 0, E)
    st = z.estado["curseduca:curseduca-segueadi"]
    assert st["relogio"] == "desconhecido" and al.vence == []     # 1 observação não classifica
    _ciclo_viva(z, ex, al, T0 + 2 * H, 1, E)
    assert st["relogio"] == "duro"                                # não avançou após prova de vida
    assert [(a, round(d, 1)) for a, _, d, _ in al.vence] == [("curseduca:curseduca-segueadi", 1.9)]
    assert al.vence[0][3] == "uv run scripts/reseed.py curseduca:curseduca-segueadi"
    _ciclo_viva(z, ex, al, T0 + 4 * H, 2, E)
    assert len(al.vence) == 1                                     # mesmo valor: sem repetir
    E2 = E + 20 * DIA                                             # o humano relogou: valor novo
    _ciclo_viva(z, ex, al, T0 + 6 * H, 3, E2)
    _ciclo_viva(z, ex, al, T0 + 8 * H, 4, E2)
    assert st["relogio"] == "duro" and len(al.vence) == 1         # duro, mas longe: quieto
    _ciclo_viva(z, ex, al, E2 - 2 * DIA, 5, E2)
    assert [v[0] for v in al.vence] == ["curseduca:curseduca-segueadi"] * 2   # valor novo alerta


def test_relogio_renovavel_nunca_alerta(tmp_path):
    cursos = [_c(HOT, "hotmart-principal", "hotmart")]
    ex, al = _exec(tmp_path, cursos), FakeAlertas()
    z = _zel(tmp_path, cursos, cfg=_cfg_rapida())
    _ocioso(ex, "hotmart-principal", T0 - DIA)
    for i in range(4):                                  # o hmSsoExp AVANÇA a cada uso
        _ciclo_viva(z, ex, al, T0 + 2 * H * i, i, T0 + DIA + 2 * H * i)
    assert z.estado["hotmart:hotmart-principal"]["relogio"] == "renovavel"
    assert al.vence == []


def test_alertas_real_logins_e_preventivo_sao_essenciais_e_dizem_se_chegou():
    from maestro.alertas import Alertas

    class TG:
        def __init__(self, falha=False):
            self.msgs, self.falha = [], falha

        def send_message(self, chat, texto):
            if self.falha:
                raise RuntimeError("telegram fora")
            self.msgs.append(texto)

    tg = TG()
    a = Alertas(tg, [1])
    cmd = "uv run scripts/reseed.py kiwify:kiwify-principal"
    assert a.logins_pendentes(["kiwify:kiwify-principal"], cmd) is True
    assert "login necessário" in tg.msgs[-1] and cmd in tg.msgs[-1]
    assert a.logins_pendentes(["kiwify:kiwify-principal"], cmd) is True
    assert len(tg.msgs) == 2                     # dedup é do agendador (persistido), não daqui
    assert a.sessao_vence("curseduca:x", "2027-01-15 10:00", 2.4, "uv run scripts/reseed.py "
                          "curseduca:x") is True
    assert "2,4 dia" in tg.msgs[-1] and "curseduca:x" in tg.msgs[-1]
    assert Alertas(None, [1]).logins_pendentes(["a:b"], "c") is True       # sem canal: só log
    assert Alertas(TG(falha=True), [1]).logins_pendentes(["a:b"], "c") is False


# ==========================================================================================
# M4 — mtime da sessão depois da morte dispara o zelo; viva rearma o latch de reseed
# ==========================================================================================
def test_mtime_da_sessao_apos_morte_dispara_zelar_e_viva_rearma_o_latch(tmp_path):
    sess = tmp_path / "aula" / ".kiwify-session.json"
    sess.parent.mkdir(parents=True)
    sess.write_text("{}")
    os.utime(sess, (T0 - 2 * DIA, T0 - 2 * DIA))
    cursos = [_c(KIW, "kiwify-principal", "kiwify", str(sess)),
              _c(KIW2, "kiwify-principal", "kiwify", str(sess))]
    estado = {
        KIW: {"irredutivel": True, "ultima_causa": "escalar_reseed", "esgotado_avisado": True,
              "fase": captura.FASE_NOVO, "_morte_ciclo": T0 - 10 * H},
        KIW2: {"irredutivel": True, "benched_exit5": True, "ultima_causa": "relancar",
               "fase": captura.FASE_NOVO},
    }
    ex, z, al = _exec(tmp_path, cursos), _zel(tmp_path, cursos), FakeAlertas()
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0, estado=estado, alertas=al)
    ex.sp.terminar(0, 3, "morta")
    _passo(z, ex, T0 + 60, estado=estado, alertas=al)
    _passo(z, ex, T0 + 5 * H, estado=estado, alertas=al)
    assert len(ex.sp.zelos()) == 1                            # morta: parada
    assert estado[KIW]["irredutivel"] is True
    os.utime(sess, (T0 + 6 * H, T0 + 6 * H))                  # o humano rodou o reseed
    _passo(z, ex, T0 + 6 * H + 60, estado=estado, alertas=al)
    assert len(ex.sp.zelos()) == 2                            # zela JÁ (sem esperar intervalo)
    ex.sp.terminar(1, 0, "viva")
    _passo(z, ex, T0 + 6 * H + 120, estado=estado, alertas=al)
    assert z.estado["kiwify:kiwify-principal"]["status"] == "viva"
    assert "irredutivel" not in estado[KIW] and "esgotado_avisado" not in estado[KIW]
    assert estado[KIW]["fase"] == captura.FASE_NOVO
    assert estado[KIW2]["irredutivel"] is True               # bench de exit-5 não é do zelador


def test_mtime_mexido_mas_segue_morta_nao_vira_loop(tmp_path):
    sess = tmp_path / "aula" / ".kiwify-session.json"
    sess.parent.mkdir(parents=True)
    sess.write_text("{}")
    os.utime(sess, (T0 - 2 * DIA, T0 - 2 * DIA))
    cursos = [_c(KIW, "kiwify-principal", "kiwify", str(sess))]
    ex, z, al = _exec(tmp_path, cursos), _zel(tmp_path, cursos), FakeAlertas()
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0, alertas=al)
    ex.sp.terminar(0, 3, "morta")
    _passo(z, ex, T0 + 60, alertas=al)
    os.utime(sess, (T0 + H, T0 + H))
    _passo(z, ex, T0 + H + 60, alertas=al)
    ex.sp.terminar(1, 3, "morta")
    for dt in (H + 120, 2 * H, 5 * H):
        _passo(z, ex, T0 + dt, alertas=al)
    assert len(ex.sp.zelos()) == 2                            # 1 zelo por mudança, não um loop
    assert len(al.logins) == 1


# ==========================================================================================
# STOA na árvore dela; status JSON sem credencial; o loop sobrevive
# ==========================================================================================
def test_zelar_da_stoa_roda_na_arvore_da_stoa(tmp_path):
    cursos = [_c(STOA, "stoa-principal", "stoa")]
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    _ocioso(ex, "stoa-principal", T0 - DIA)
    _passo(z, ex, T0)
    call = ex.sp.calls[0]
    assert call["cmd"] == [PY, "-m", "motor.zelador", "stoa", STOA, "--conta", "stoa-principal"]
    assert call["cwd"] == str(tmp_path / "stoa")
    assert call["env"]["PYTHONPATH"] == str(tmp_path / "stoa")
    assert call["env"]["MOTOR_BROWSER"] == "chromium" and "HEADLESS" not in call["env"]
    assert call["env"]["CHROME_USER_DATA_DIR"] == ".chrome-profile-stoa"
    assert call["env"]["_ATHENA_MOTOR_STDERR"] != ex._stderr_path("stoa-principal")


def test_status_json_sem_credencial_escrita_atomica(tmp_path, monkeypatch):
    cursos = [_c(KIW, "kiwify-principal", "kiwify")]
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0)
    ex.sp.terminar(0, 5, "inconclusiva", detalhe="cookie TGC=TGT-SEGREDO-1 token eyJhbGciOi.x.y",
                   antes="Traceback: cookie TGC=TGT-SEGREDO-1 Bearer eyJhbGciOi.segredo.z\n")
    trocas = []
    real = os.replace
    monkeypatch.setattr(zmod.os, "replace", lambda a, b: (trocas.append((a, b)), real(a, b))[1])
    _passo(z, ex, T0 + 60)
    path = str(tmp_path / "sessoes-status.json")
    assert trocas and trocas[-1][1] == path
    assert os.path.dirname(trocas[-1][0]) == os.path.dirname(path)   # tmp no MESMO dir
    bruto = open(path).read()
    for proibido in ("TGT-SEGREDO", "eyJ", "cookie", "TGC=", "Bearer", "token"):
        assert proibido not in bruto
    assert z.estado["kiwify:kiwify-principal"]["detalhe"] == "detalhe descartado"
    conta = _status(tmp_path)["contas"][0]
    assert {"conta", "plataforma", "url", "status", "resultado", "provado_em", "expira_em",
            "vence_em_h", "relogio", "mortes_seguidas", "detalhe", "decisao"} <= set(conta)
    assert conta["resultado"] == "inconclusiva"
    antes = open(path).read()

    def explode(*a, **k):
        raise OSError("disco cheio")

    monkeypatch.setattr(zmod.json, "dump", explode)
    _passo(z, ex, T0 + 120)                                    # falhar ao gravar não derruba
    assert open(path).read() == antes                          # nem deixa meia-escrita
    assert [n for n in os.listdir(tmp_path) if n.endswith(".tmp")] == []


def test_estado_restaurado_do_status_no_restart(tmp_path):
    cursos = [_c(KIW, "kiwify-principal", "kiwify")]
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0)
    ex.sp.terminar(0, 0, "viva")
    _passo(z, ex, T0 + 60)
    prox = z.estado["kiwify:kiwify-principal"]["_t"]["proximo_ts"]
    z2 = _zel(tmp_path, cursos, seed=99)                       # restart: outro sorteio
    st2 = z2.estado["kiwify:kiwify-principal"]
    assert st2["status"] == "viva" and st2["_t"]["proximo_ts"] == prox
    _passo(z2, ex, prox - 60)
    assert len(ex.sp.zelos()) == 1                             # não re-zela no boot


class _ExecMinimo:
    def __init__(self):
        self.disparos = []

    def curso_ativo(self, curso, **kw):
        return False

    def disparar(self, curso):
        self.disparos.append(curso)
        return f"local_iniciada:{curso}"


class _Voz:
    def avisar_acao(self, acao):
        pass

    def escalar(self, problema, pedido):
        pass


class _ZeladorContador:
    def __init__(self, explode=False):
        self.chamadas, self.explode, self.ordem = [], explode, None

    def passo(self, executor, estado, *, agora, alertas, espinha=None, **kw):
        self.kw = kw
        self.chamadas.append(agora)
        self.ordem = list(executor.disparos)
        if self.explode:
            raise RuntimeError("bug no zelador")


def test_ciclo_roda_o_zelador_depois_da_captura_e_sobrevive_se_ele_explode():
    cursos = [_c(KIW, "kiwify-principal", "kiwify")]
    ex, zel = _ExecMinimo(), _ZeladorContador(explode=True)
    res = athena_local.ciclo_local(cursos, ex, lambda c: (0, 10), _Voz(), {}, {}, agora=T0,
                                   zelador=zel)
    assert zel.chamadas == [T0]
    assert zel.ordem == [KIW]                                  # a captura teve a vez primeiro
    assert zel.kw["ativos"] == frozenset({KIW})                # só o que passou gate/controle
    assert res is not None


def test_loop_de_zelo_sobrevive_a_ciclo_que_explode():
    cursos = [_c(KIW, "kiwify-principal", "kiwify")]

    class ControleUmaVezQuebrado:
        def __init__(self):
            self.n = 0

        def filtrar_cursos(self, cs, path=None):
            self.n += 1
            if self.n == 1:
                raise ValueError("controle.yaml corrompido")
            return list(cs)

    async def sono(_):
        return None

    zel = _ZeladorContador()
    asyncio.run(athena_local.rodar(cursos, _ExecMinimo(), lambda c: (0, 10), _Voz(),
                                   sleep=sono, intervalo_s=0, max_iters=3,
                                   controle=ControleUmaVezQuebrado(), controle_path="x",
                                   zelador=zel))
    assert len(zel.chamadas) == 2                              # ciclo 1 estourou; 2 e 3 zelaram


def test_sem_zelador_o_ciclo_nao_muda():
    cursos = [_c(KIW, "kiwify-principal", "kiwify")]
    ex = _ExecMinimo()
    athena_local.ciclo_local(cursos, ex, lambda c: (0, 10), _Voz(), {}, {}, agora=T0)
    assert ex.disparos == [KIW]


def test_watchdog_do_zelo_travado_sigterm_depois_sigkill_e_lock_so_sai_morto(tmp_path):
    import signal
    cursos = [_c(KIW, "kiwify-principal", "kiwify")]
    sinais = []
    ex = _exec(tmp_path, cursos, zelo_timeout_s=600, zelo_grace_s=60,
               sinal_fn=lambda pid, sig, grupo: sinais.append((pid, sig, grupo)))
    z = _zel(tmp_path, cursos)
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0)
    pid = ex.sp.calls[0]["proc"].pid
    _passo(z, ex, T0 + 300)
    assert sinais == []
    _passo(z, ex, T0 + 601)
    assert sinais == [(pid, signal.SIGTERM, False)]           # 1º: o zelador fecha o navegador
    _passo(z, ex, T0 + 700)
    assert sinais[-1] == (pid, signal.SIGKILL, True)          # depois: o grupo inteiro
    assert _lock(ex, "kiwify-principal")["pid"] == pid        # vivo => lock fica
    ex.sp.calls[0]["proc"].encerrar(-9)
    _passo(z, ex, T0 + 760)
    assert not os.path.exists(ex._lock_path("kiwify-principal"))
    st = z.estado["kiwify:kiwify-principal"]
    assert st["resultado"] == "inconclusiva" and st["status"] == "desconhecida"


def test_viva_sem_acao_humana_depois_do_latch_nao_rearma(tmp_path):
    """Latch de reseed com o arquivo de sessão MAIS VELHO que ele: ninguém relogou. Rearmar
    num viva desses abriria um laço (captura morre -> latch -> zelo viva -> rearma -> ...),
    com um `sessao_expirada` por volta. O rearme exige a marca de ação humana."""
    sess = tmp_path / "aula" / ".kiwify-session.json"
    sess.parent.mkdir(parents=True)
    sess.write_text("{}")
    os.utime(sess, (T0 - 2 * DIA, T0 - 2 * DIA))
    cursos = [_c(KIW, "kiwify-principal", "kiwify", str(sess))]
    estado = {KIW: {"irredutivel": True, "ultima_causa": "escalar_reseed",
                    "fase": captura.FASE_NOVO, "_morte_ciclo": T0 - H}}
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0, estado=estado)
    ex.sp.terminar(0, 0, "viva")
    _passo(z, ex, T0 + 60, estado=estado)
    assert estado[KIW]["irredutivel"] is True


def test_viva_depois_de_reseed_humano_rearma_mesmo_sem_o_zelador_ter_visto_a_morte(tmp_path):
    """A captura morreu (latch) e o humano relogou (arquivo mais novo que o latch) sem o
    zelador ter provado a morte: o próximo zelo viva rearma — sem esperar restart."""
    sess = tmp_path / "aula" / ".kiwify-session.json"
    sess.parent.mkdir(parents=True)
    sess.write_text("{}")
    os.utime(sess, (T0 - 30 * 60, T0 - 30 * 60))
    cursos = [_c(KIW, "kiwify-principal", "kiwify", str(sess))]
    estado = {KIW: {"irredutivel": True, "ultima_causa": "escalar_reseed",
                    "esgotado_avisado": True, "fase": captura.FASE_NOVO,
                    "_morte_ciclo": T0 - H}}
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0, estado=estado)
    ex.sp.terminar(0, 0, "viva")
    _passo(z, ex, T0 + 60, estado=estado)
    assert "irredutivel" not in estado[KIW]


def test_zelador_so_zela_o_que_a_captura_pode_rodar(tmp_path):
    """Plataforma desligada no gate ou conta pausada no controle.yaml: o zelador também não
    abre o perfil dela (o operador desligou por um motivo)."""
    cursos = [_c(KIW, "kiwify-principal", "kiwify"), _c(CUR, "curseduca-segueadi", "curseduca")]
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    for c in cursos:
        _ocioso(ex, c.conta, T0 - DIA)
    z.passo(ex, {}, agora=T0, alertas=FakeAlertas(), ativos=frozenset({CUR}))
    assert [c["cmd"][3] for c in ex.sp.zelos()] == ["curseduca"]
    assert {c["conta"]: c["decisao"] for c in _status(tmp_path)["contas"]}[
        "kiwify-principal"] == "fora-do-gate"


def test_executor_recusa_segundo_zelo_mesmo_de_outra_conta(tmp_path):
    """A trava de 1-por-vez mora TAMBÉM no executor (defesa em profundidade: um agendador
    com defeito não abre dois navegadores de zelo)."""
    cursos = [_c(KIW, "kiwify-principal", "kiwify"), _c(CUR, "curseduca-segueadi", "curseduca")]
    ex = _exec(tmp_path, cursos)
    ex.disparar_zelo("kiwify:kiwify-principal", cursos[0], agora=T0)
    with pytest.raises(captura.ContaOcupada):
        ex.disparar_zelo("curseduca:curseduca-segueadi", cursos[1], agora=T0)
    assert len(ex.sp.calls) == 1
    assert not os.path.exists(ex._lock_path("curseduca-segueadi"))
