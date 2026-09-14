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

Revisão do P7 — o dublê passou a modelar dois mecanismos que ele ignorava (e por isso a
suíte deixou passar o rearme sem humano): num VIVA o motor real RE-PERSISTE o arquivo de
sessão (`capture_state` do `ensure_session`: conteúdo novo, mtime = a hora do zelo); e a
linha de uma MORTE na Stoa/Alpaclass traz `prova: positiva` (a sonda tri-estado do motor).
Rodada 10 — e o Chrome do zelo mexe na raiz do perfil (Singleton*, `Local State`) em
QUALQUER abertura, não só num viva: morta, inconclusiva, teto, watchdog (o mecanismo que
escondia o 2º achado bloqueante — o zelo inconclusivo re-disparando o seguinte).
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


_AUTO = object()


class FakeSpawn:
    def __init__(self, mundo=None):
        self.mundo = mundo or Mundo()
        self.calls = []
        self.agora = None                 # a hora (falsa) do passo em curso — `_passo` a põe

    def __call__(self, cmd, *, env, cwd):
        proc = self.mundo.novo()
        self.calls.append({"cmd": list(cmd), "env": dict(env), "cwd": cwd, "proc": proc,
                           "t": self.agora})
        return proc

    def zelos(self):
        return [c for c in self.calls if c["cmd"][2] == "motor.zelador"]

    @staticmethod
    def _absol(p, cwd):
        return p if os.path.isabs(p) else os.path.join(cwd, p)

    def _caminhos(self, call):
        env, cwd, plat = call["env"], call["cwd"], call["cmd"][3]
        spec = captura._PLATAFORMAS.get(plat)
        sess = (env.get(spec.session_env) if spec is not None and spec.session_env else "") \
            or zmod._SESSAO_PADRAO.get(plat, "")
        perfil = env.get("CHROME_USER_DATA_DIR") or ""
        return (self._absol(perfil, cwd) if perfil else ""), (self._absol(sess, cwd) if sess else "")

    def _o_chrome_mexe_no_perfil(self, call):
        """O MECANISMO de TODA abertura real: o Chrome cria/apaga Singleton* e grava `Local
        State` na raiz do perfil — mtime da raiz = a hora do zelo (+30 s). Vale para qualquer
        desfecho do navegador (viva, morta, inconclusiva, teto, watchdog)."""
        if call["t"] is None:
            return
        quando = call["t"] + 30
        perfil, _ = self._caminhos(call)
        if perfil and os.path.isdir(perfil):
            os.utime(perfil, (quando, quando))

    def _o_viva_mexe_no_disco(self, call):
        """O MECANISMO do viva real: além do perfil, o `ensure_session` RE-PERSISTE o arquivo
        de sessão (`capture_state`: CONTEÚDO novo — cookies/tokens renovados — e mtime = a hora
        do zelo). Só o que existe (o zelador real nem abre perfil inexistente)."""
        if call["t"] is None:
            return
        quando = call["t"] + 30
        _, sess = self._caminhos(call)
        if sess and os.path.exists(sess):
            with open(sess, "w") as f:
                json.dump({"capture_state": quando}, f)
            os.utime(sess, (quando, quando))

    def terminar(self, i, code, resultado=None, *, expira=None, detalhe="x", antes="",
                 prova=_AUTO):
        """Encerra o i-ésimo spawn; se `resultado`, grava a linha ZELADOR no tee (como o
        motor/zelador.py real imprime no stdout, que o `_spawn_popen` tee'a). `prova` (default)
        = a do motor novo: "positiva" numa morte da Stoa/Alpaclass; None nas demais."""
        call = self.calls[i]
        plat = call["cmd"][3] if len(call["cmd"]) > 3 else ""
        if prova is _AUTO:
            prova = "positiva" if (resultado == "morta" and plat in zmod.SONDA_TRI_ESTADO) \
                else None
        path = call["env"].get("_ATHENA_MOTOR_STDERR")
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(antes)
                if resultado is not None:
                    linha = {"plataforma": plat, "conta": call["cmd"][6],
                             "resultado": resultado, "detalhe": detalhe, "expira_em": expira,
                             "vence_em_h": None, "provado_em": None, "prova": prova}
                    f.write("ZELADOR " + json.dumps(linha) + "\n")
        if plat == "" or resultado not in ("sem-perfil", "sem-sessao-salva", "ocupado"):
            self._o_chrome_mexe_no_perfil(call)     # o motor abriu o navegador
        if resultado == "viva" and code == 0:
            self._o_viva_mexe_no_disco(call)
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


def _passo(z, ex, agora, *, estado=None, alertas=None, ativos=None):
    ex.sp.agora = agora
    return z.passo(ex, {} if estado is None else estado, agora=agora,
                   alertas=alertas if alertas is not None else FakeAlertas(), ativos=ativos)


def _carimbar(ex, conta, ts, plataforma="kiwify"):
    """O que `scripts/reseed.py` (motor/conta_lock.py::carimbar_reseed) grava depois de um
    login humano concluído — no formato do espelho."""
    import hashlib
    slug = hashlib.sha256(conta.encode("utf-8")).hexdigest()[:16]
    with open(os.path.join(ex._lock_dir, slug + ".reseed.json"), "w") as f:
        json.dump({"conta": conta, "plataforma": plataforma, "ts": ts, "dono": "reseed"}, f)


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
    # canal LIGADO de propósito: o assunto aqui é o CONTRATO de retorno do zelador
    # (True = nada a re-tentar), não a decisão de 14/09 de calar o canal por padrão.
    a = Alertas(tg, [1], nivel="essencial")
    cmd = "uv run scripts/reseed.py kiwify:kiwify-principal"
    assert a.logins_pendentes(["kiwify:kiwify-principal"], cmd) is True
    assert "login necessário" in tg.msgs[-1] and cmd in tg.msgs[-1]
    assert a.logins_pendentes(["kiwify:kiwify-principal"], cmd) is True
    assert len(tg.msgs) == 2                     # dedup é do agendador (persistido), não daqui
    assert a.sessao_vence("curseduca:x", "2027-01-15 10:00", 2.4, "uv run scripts/reseed.py "
                          "curseduca:x") is True
    assert "2,4 dia" in tg.msgs[-1] and "curseduca:x" in tg.msgs[-1]
    assert Alertas(None, [1]).logins_pendentes(["a:b"], "c") is True       # sem canal: só log
    # canal ligado + Telegram fora = False (o zelador re-tenta). É o único caso em que
    # False é a resposta certa: houve canal e ele FALHOU.
    assert Alertas(TG(falha=True), [1], nivel="essencial").logins_pendentes(["a:b"], "c") is False
    # e no padrão de hoje (mudo) o retorno é True: não há nada a re-tentar, por decisão.
    assert Alertas(TG(), [1]).logins_pendentes(["a:b"], "c") is True


# ==========================================================================================
# M4 — o rearme exige o CARIMBO do reseed humano; o mtime só re-sonda (achado bloqueante)
# ==========================================================================================
def _latch_kiwify(tmp_path, morte_ciclo, sess_mtime):
    sess = tmp_path / "aula" / ".kiwify-session.json"
    sess.parent.mkdir(parents=True, exist_ok=True)
    sess.write_text("{}")
    os.utime(sess, (sess_mtime, sess_mtime))
    perfil = tmp_path / "aula" / ".chrome-profile-kiwify-principal"
    perfil.mkdir(parents=True, exist_ok=True)
    os.utime(perfil, (sess_mtime, sess_mtime))
    cursos = [_c(KIW, "kiwify-principal", "kiwify", str(sess)),
              _c(KIW2, "kiwify-principal", "kiwify", str(sess))]
    estado = {
        KIW: {"irredutivel": True, "ultima_causa": "escalar_reseed", "esgotado_avisado": True,
              "fase": captura.FASE_NOVO, "_morte_ciclo": morte_ciclo},
        KIW2: {"irredutivel": True, "benched_exit5": True, "ultima_causa": "relancar",
               "fase": captura.FASE_NOVO},
    }
    return sess, perfil, cursos, estado


def test_carimbo_do_reseed_apos_morte_dispara_zelar_e_viva_rearma_o_latch(tmp_path):
    sess, _, cursos, estado = _latch_kiwify(tmp_path, T0 - 10 * H, T0 - 2 * DIA)
    ex, z, al = _exec(tmp_path, cursos), _zel(tmp_path, cursos), FakeAlertas()
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0, estado=estado, alertas=al)
    ex.sp.terminar(0, 3, "morta")
    _passo(z, ex, T0 + 60, estado=estado, alertas=al)
    _passo(z, ex, T0 + 5 * H, estado=estado, alertas=al)
    assert len(ex.sp.zelos()) == 1                            # morta: parada
    assert estado[KIW]["irredutivel"] is True
    _carimbar(ex, "kiwify-principal", T0 + 6 * H)             # o humano rodou o reseed.py
    os.utime(sess, (T0 + 6 * H, T0 + 6 * H))
    _passo(z, ex, T0 + 6 * H + 60, estado=estado, alertas=al)
    assert len(ex.sp.zelos()) == 2                            # zela JÁ (sem esperar intervalo)
    ex.sp.terminar(1, 0, "viva")
    _passo(z, ex, T0 + 6 * H + 120, estado=estado, alertas=al)
    assert z.estado["kiwify:kiwify-principal"]["status"] == "viva"
    assert "irredutivel" not in estado[KIW] and "esgotado_avisado" not in estado[KIW]
    assert estado[KIW]["fase"] == captura.FASE_NOVO
    assert estado[KIW2]["irredutivel"] is True               # bench de exit-5 não é do zelador


def test_arquivo_reescrito_apos_morte_resonda_mas_sem_carimbo_humano_nunca_rearma(tmp_path):
    """Um `--reseed` AVULSO (sem carimbo) ou a captura de outro curso REESCREVE o arquivo de
    sessão: o zelador RE-SONDA a conta aguardando-humano (a sessão pode ter voltado) e, viva,
    sai do aguardando — mas a CAPTURA travada só rearma com o carimbo do reseed humano."""
    sess, _, cursos, estado = _latch_kiwify(tmp_path, T0 - 10 * H, T0 - 2 * DIA)
    ex, z, al = _exec(tmp_path, cursos), _zel(tmp_path, cursos), FakeAlertas()
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0, estado=estado, alertas=al)
    ex.sp.terminar(0, 3, "morta")
    _passo(z, ex, T0 + 60, estado=estado, alertas=al)
    sess.write_text('{"cookies": ["login-novo"]}')           # relogaram, sem reseed.py
    _passo(z, ex, T0 + 6 * H + 60, estado=estado, alertas=al)
    assert len(ex.sp.zelos()) == 2                            # re-sonda já
    ex.sp.terminar(1, 0, "viva")
    _passo(z, ex, T0 + 6 * H + 120, estado=estado, alertas=al)
    assert z.estado["kiwify:kiwify-principal"]["status"] == "viva"
    assert estado[KIW]["irredutivel"] is True                # sem carimbo: não rearma


def test_vivas_seguidos_sem_humano_nunca_rearmam_mesmo_com_o_zelo_regravando_o_arquivo(
        tmp_path):
    """O achado BLOQUEANTE da revisão, com o dublê que modela o mecanismo: cada viva
    re-persiste o arquivo de sessão e mexe no perfil (mtime = a hora do zelo), e a captura
    de OUTRO curso da conta também mexe. Com a marca antiga (mtime), o 2º viva rearmava o
    latch sem ninguém ter logado (e a captura morria de novo: um alerta por volta)."""
    sess, perfil, cursos, estado = _latch_kiwify(tmp_path, T0 - H, T0 - 2 * DIA)
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    t = T0
    for n in range(3):
        _passo(z, ex, t, estado=estado)
        assert len(ex.sp.zelos()) == n + 1
        ex.sp.terminar(len(ex.sp.calls) - 1, 0, "viva")
        _passo(z, ex, t + 60, estado=estado)
        assert os.path.getmtime(sess) == t + 30               # o dublê regravou o arquivo
        # a captura de outro curso da conta roda entre os zelos e mexe no perfil/arquivo
        for p in (sess, perfil):
            os.utime(p, (t + 2 * H, t + 2 * H))
        t += 13 * H
        assert estado[KIW].get("irredutivel") is True, f"rearmou sem humano no viva {n + 1}"


def test_carimbo_anterior_ao_latch_nao_rearma_e_nao_vira_laco(tmp_path):
    """O humano relogou (carimbo), a captura rearmou e MORREU DE NOVO depois do login: o
    próximo viva NÃO rearma por aquele mesmo carimbo — senão: morre, rearma, morre..."""
    sess, _, cursos, estado = _latch_kiwify(tmp_path, T0 - H, T0 - 2 * DIA)
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    _carimbar(ex, "kiwify-principal", T0 - 3 * H)             # login humano ANTES do latch
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0, estado=estado)
    ex.sp.terminar(0, 0, "viva")
    _passo(z, ex, T0 + 60, estado=estado)
    assert estado[KIW]["irredutivel"] is True


def test_carimbo_novo_poe_a_unidade_na_frente_mesmo_viva_e_so_uma_vez(tmp_path):
    """A captura travou (sessão morta) sem o zelador ter visto; o humano rodou o reseed.py:
    o zelo vem JÁ (não no fim do intervalo de 6–12 h), rearma, e o mesmo carimbo não dispara
    outro zelo — nem depois de um restart (o `carimbo_visto` é persistido)."""
    sess, _, cursos, estado = _latch_kiwify(tmp_path, T0 - H, T0 - 2 * DIA)
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0, estado=estado)
    ex.sp.terminar(0, 0, "viva")                              # viva, sem humano: segue travado
    _passo(z, ex, T0 + 60, estado=estado)
    assert estado[KIW]["irredutivel"] is True
    _carimbar(ex, "kiwify-principal", T0 + H)
    _passo(z, ex, T0 + H + 60, estado=estado)                 # bem antes do intervalo (>= 6 h)
    assert len(ex.sp.zelos()) == 2
    ex.sp.terminar(1, 0, "viva")
    _passo(z, ex, T0 + H + 120, estado=estado)
    assert "irredutivel" not in estado[KIW]
    z2 = _zel(tmp_path, cursos)                               # restart do daemon
    for dt in (2 * H, 3 * H):
        _passo(z2, ex, T0 + dt, estado=estado)
    assert len(ex.sp.zelos()) == 2                            # o carimbo já teve o seu zelo


def test_arquivo_reescrito_mas_segue_morta_nao_vira_loop(tmp_path):
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
    sess.write_text('{"cookies": ["outro"]}')
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


def test_viva_depois_de_reseed_humano_rearma_mesmo_sem_o_zelador_ter_visto_a_morte(tmp_path):
    """A captura morreu (latch) e o humano relogou pelo reseed.py (carimbo mais novo que o
    latch) sem o zelador ter provado a morte: o próximo zelo viva rearma — sem restart."""
    sess = tmp_path / "aula" / ".kiwify-session.json"
    sess.parent.mkdir(parents=True)
    sess.write_text("{}")
    os.utime(sess, (T0 - 30 * 60, T0 - 30 * 60))
    cursos = [_c(KIW, "kiwify-principal", "kiwify", str(sess))]
    estado = {KIW: {"irredutivel": True, "ultima_causa": "escalar_reseed",
                    "esgotado_avisado": True, "fase": captura.FASE_NOVO,
                    "_morte_ciclo": T0 - H}}
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    _carimbar(ex, "kiwify-principal", T0 - 30 * 60)
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0, estado=estado)
    ex.sp.terminar(0, 0, "viva")
    _passo(z, ex, T0 + 60, estado=estado)
    assert "irredutivel" not in estado[KIW]


def test_executor_le_o_carimbo_no_formato_do_motor_e_nada_mais(tmp_path):
    """ESPELHO de motor/conta_lock.py::carimbar_reseed: `<lock_dir>/<sha256[:16]>.reseed.json`
    com `ts`. Ilegível, de outra conta ou sem `ts` numérico = None (na dúvida, não rearma);
    e o carimbo NUNCA é lido como lock (a conta não fica ocupada por ele)."""
    import hashlib
    cursos = [_c(KIW, "kiwify-principal", "kiwify")]
    ex = _exec(tmp_path, cursos)
    assert ex.carimbo_reseed("kiwify-principal") is None
    _carimbar(ex, "kiwify-principal", T0)
    slug = hashlib.sha256(b"kiwify-principal").hexdigest()[:16]
    assert os.path.exists(os.path.join(ex._lock_dir, slug + ".reseed.json"))
    assert ex.carimbo_reseed("kiwify-principal") == T0
    assert ex.conta_ocupada("kiwify-principal") is False
    assert ex.contas_livres(["kiwify-principal"]) is True
    assert ex.zelo_em_curso() is False
    for lixo in ("{", json.dumps({"conta": "outra", "ts": T0}),
                 json.dumps({"conta": "kiwify-principal", "ts": "ontem"}),
                 json.dumps({"conta": "kiwify-principal", "ts": True})):
        with open(os.path.join(ex._lock_dir, slug + ".reseed.json"), "w") as f:
            f.write(lixo)
        assert ex.carimbo_reseed("kiwify-principal") is None


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


# ==========================================================================================
# REVISÃO DO P7 — item 2: queda de rede nunca vira morte (Stoa/Alpaclass exigem prova positiva)
# ==========================================================================================
@pytest.mark.parametrize("plat,url,conta", [(STOA, None, "stoa-principal"),
                                            ("https://fsp.alpaclass.com/", None,
                                             "alpaclass-principal")])
def test_morte_sem_prova_positiva_na_stoa_e_alpaclass_e_inconclusiva(tmp_path, plat, url, conta):
    """Um motor/árvore ANTIGO (sem a sonda tri-estado) diz "morta" numa queda de internet.
    Sem `prova: positiva` o daemon não conta: nem suspeita, nem alerta."""
    p = "stoa" if plat == STOA else "alpaclass"
    cursos = [_c(plat, conta, p)]
    ex, z, al = _exec(tmp_path, cursos), _zel(tmp_path, cursos), FakeAlertas()
    _ocioso(ex, conta, T0 - DIA)
    _passo(z, ex, T0, alertas=al)
    ex.sp.terminar(0, 3, "morta", detalhe="SessionDeadError", prova=None)
    _passo(z, ex, T0 + 60, alertas=al)
    st = z.estado[f"{p}:{conta}"]
    assert st["resultado"] == "inconclusiva" and st["mortes_seguidas"] == 0
    assert st["status"] == "desconhecida" and st["detalhe"] == "morte sem prova positiva"
    assert al.logins == []


def test_confirmacao_da_stoa_exige_duas_provas_positivas_nunca_falhas_de_rede(tmp_path):
    cursos = [_c(STOA, "stoa-principal", "stoa")]
    ex, z, al = _exec(tmp_path, cursos), _zel(tmp_path, cursos), FakeAlertas()
    _ocioso(ex, "stoa-principal", T0 - DIA)
    _passo(z, ex, T0, alertas=al)
    ex.sp.terminar(0, 3, "morta")                               # 1ª prova POSITIVA
    _passo(z, ex, T0 + 60, alertas=al)
    st = z.estado["stoa:stoa-principal"]
    assert st["status"] == "morte-suspeita"
    _passo(z, ex, T0 + 31 * 60, alertas=al)
    ex.sp.terminar(1, 5, "inconclusiva", detalhe="SessionProbeInconclusiveError")  # rede caiu
    _passo(z, ex, T0 + 32 * 60, alertas=al)
    assert st["status"] == "morte-suspeita" and al.logins == []
    _passo(z, ex, T0 + 32 * 60 + H, alertas=al)
    ex.sp.terminar(2, 3, "morta", prova=None)                   # "morta" sem prova: não conta
    _passo(z, ex, T0 + 32 * 60 + H + 60, alertas=al)
    assert st["status"] == "morte-suspeita" and al.logins == []
    _passo(z, ex, T0 + 32 * 60 + 3 * H + 120, alertas=al)
    ex.sp.terminar(3, 3, "morta")                               # 2ª prova POSITIVA
    _passo(z, ex, T0 + 32 * 60 + 3 * H + 180, alertas=al)
    assert st["status"] == "aguardando-humano"
    assert [a for a, _ in al.logins] == [("stoa:stoa-principal",)]


def test_hubla_sem_sonda_tri_estado_segue_a_confirmacao_de_antes(tmp_path):
    """A Hubla não tem a sonda tri-estado (a rede dela já sobe inconclusiva): a morte dela
    não carrega `prova` e continua contando para a confirmação espaçada."""
    cursos = [_c(HUB, "hubla-principal", "hubla")]
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    _ocioso(ex, "hubla-principal", T0 - DIA)
    _passo(z, ex, T0)
    ex.sp.terminar(0, 3, "morta")
    _passo(z, ex, T0 + 60)
    assert z.estado["hubla:hubla-principal"]["status"] == "morte-suspeita"


def test_a_linha_do_zelador_traz_a_prova_e_nada_fora_da_lista():
    linha = captura.ler_linha_zelador('ZELADOR {"resultado": "morta", "prova": "positiva", '
                                      '"cookie": "x"}')
    assert linha == {"resultado": "morta", "prova": "positiva"}


# ==========================================================================================
# REVISÃO DO P7 — item 3: o vigia do zelo travado sobrevive ao restart do daemon
# ==========================================================================================
_CMD_ZELO = "/opt/aula/.venv/bin/python -m motor.zelador kiwify {} --conta kiwify-principal"


def _zelo_e_restart(tmp_path, comando_fn=None, **kw):
    """Um zelo disparado por um daemon que REINICIOU no meio (o `_zelos` em memória morreu
    com ele): devolve (ex2, proc do zelo, sinais) — ex2 é a encarnação nova."""
    import signal as _s  # noqa: F401
    cursos = [_c(KIW, "kiwify-principal", "kiwify")]
    sinais = []
    ex = _exec(tmp_path, cursos, zelo_timeout_s=600, zelo_grace_s=60,
               sinal_fn=lambda pid, sig, grupo: sinais.append((pid, sig, grupo)), **kw)
    z = _zel(tmp_path, cursos)
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0)
    proc = ex.sp.calls[0]["proc"]
    cmd = comando_fn or (lambda pid: _CMD_ZELO.format(KIW) if pid == proc.pid else None)
    ex2 = _exec(tmp_path, cursos, sp=ex.sp, zelo_timeout_s=600, zelo_grace_s=60,
                sinal_fn=lambda pid, sig, grupo: sinais.append((pid, sig, grupo)),
                comando_fn=cmd)
    return ex2, proc, sinais


def test_zelo_orfao_travado_depois_do_restart_leva_sigterm_e_sigkill_pelo_lock(tmp_path):
    import signal
    ex2, proc, sinais = _zelo_e_restart(tmp_path)
    ex2.vigiar_zelos_orfaos(T0 + 300)
    assert sinais == []                                        # dentro do teto: nada
    assert _lock(ex2, "kiwify-principal")["pid"] == proc.pid
    ex2.vigiar_zelos_orfaos(T0 + 601)
    assert sinais == [(proc.pid, signal.SIGTERM, False)]
    assert _lock(ex2, "kiwify-principal")["sinal"] == ["TERM", T0 + 601]   # gravado no lock
    ex2.vigiar_zelos_orfaos(T0 + 630)
    assert len(sinais) == 1                                    # carência: não repete o TERM
    ex2.vigiar_zelos_orfaos(T0 + 662)
    assert sinais[-1] == (proc.pid, signal.SIGKILL, True)      # o GRUPO do zelo
    assert _lock(ex2, "kiwify-principal")["pid"] == proc.pid   # vivo => o lock fica
    proc.encerrar(-9)
    ex2.vigiar_zelos_orfaos(T0 + 700)
    assert not os.path.exists(ex2._lock_path("kiwify-principal"))   # morto: a conta volta


def test_sinal_gravado_continua_a_escalada_depois_de_outro_restart(tmp_path):
    """O TERM foi mandado pela encarnação ANTERIOR (em memória) e gravado no lock: a nova
    não recomeça do zero — espera a carência e manda o KILL."""
    import signal
    cursos = [_c(KIW, "kiwify-principal", "kiwify")]
    sinais = []
    ex = _exec(tmp_path, cursos, zelo_timeout_s=600, zelo_grace_s=60,
               sinal_fn=lambda pid, sig, grupo: sinais.append((pid, sig, grupo)))
    z = _zel(tmp_path, cursos)
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0)
    proc = ex.sp.calls[0]["proc"]
    _passo(z, ex, T0 + 601)                                    # watchdog em memória: TERM
    assert sinais == [(proc.pid, signal.SIGTERM, False)]
    assert _lock(ex, "kiwify-principal")["sinal"] == ["TERM", T0 + 601]
    ex2 = _exec(tmp_path, cursos, sp=ex.sp, zelo_timeout_s=600, zelo_grace_s=60,
                sinal_fn=lambda pid, sig, grupo: sinais.append((pid, sig, grupo)),
                comando_fn=lambda pid: _CMD_ZELO.format(KIW))
    ex2.vigiar_zelos_orfaos(T0 + 620)
    assert len(sinais) == 1                                    # sem 2º TERM
    ex2.vigiar_zelos_orfaos(T0 + 662)
    assert sinais[-1] == (proc.pid, signal.SIGKILL, True)


def test_zelo_orfao_que_ja_saiu_solta_o_lock_para_a_conta_voltar_a_ser_zelada(tmp_path):
    """Sem este vigia, o lock de PID morto de um zelo órfão ficava para sempre numa conta
    ociosa (nenhuma captura o apagava) e o zelador a veria "ocupada" eternamente."""
    ex2, proc, sinais = _zelo_e_restart(tmp_path)
    proc.encerrar(0)
    assert ex2.contas_livres(["kiwify-principal"]) is False
    ex2.vigiar_zelos_orfaos(T0 + 120)
    assert ex2.contas_livres(["kiwify-principal"]) is True
    assert sinais == []


def test_pid_reusado_nao_leva_sinal_e_o_lock_sai(tmp_path):
    """O zelo morreu e o PID foi reusado por OUTRO processo (a linha de comando não é a do
    motor.zelador): matar seria matar um inocente; o zelo acabou — o lock sai."""
    ex2, proc, sinais = _zelo_e_restart(tmp_path, comando_fn=lambda pid: "/usr/bin/vim notas.txt")
    ex2.vigiar_zelos_orfaos(T0 + 2000)
    assert sinais == []
    assert not os.path.exists(ex2._lock_path("kiwify-principal"))


def test_zelo_orfao_sem_prova_nao_leva_sinal_e_o_lock_fica(tmp_path):
    ex2, proc, sinais = _zelo_e_restart(tmp_path, comando_fn=lambda pid: None)
    ex2.vigiar_zelos_orfaos(T0 + 5000)
    assert sinais == []
    assert _lock(ex2, "kiwify-principal")["pid"] == proc.pid


def test_intencao_orfa_do_zelador_sai_so_depois_do_teto_e_sem_navegador(tmp_path):
    cursos = [_c(KIW, "kiwify-principal", "kiwify")]
    ex = _exec(tmp_path, cursos, zelo_timeout_s=600, zelo_grace_s=60)
    perfil = tmp_path / "aula" / ".chrome-profile-kiwify-principal"
    perfil.mkdir(parents=True)
    intencao = {"pid": None, "course_url": "zelador:kiwify", "conta": "kiwify-principal",
                "ts": T0, "dono": "zelador", "zelo": "kiwify:kiwify-principal",
                "perfil": str(perfil)}
    with open(ex._lock_path("kiwify-principal"), "w") as f:
        json.dump(intencao, f)
    assert ex.zelo_em_curso() is True                          # bloqueia todos os zelos
    ex.vigiar_zelos_orfaos(T0 + 600)
    assert _lock(ex, "kiwify-principal") == intencao           # dentro do teto: fica
    chrome = ex.sp.mundo.novo()                                # o zelo subiu e deixou Chrome
    (perfil / "SingletonLock").symlink_to(f"host-{chrome.pid}")
    ex.vigiar_zelos_orfaos(T0 + 661)
    lk = _lock(ex, "kiwify-principal")
    assert lk["pid"] == chrome.pid and lk["navegador_orfao"]  # passa ao navegador
    chrome.encerrar(0)
    ex.vigiar_zelos_orfaos(T0 + 700)
    assert not os.path.exists(ex._lock_path("kiwify-principal"))
    assert ex.zelo_em_curso() is False


def test_navegador_orfao_com_prova_escala_para_sigkill_depois_da_carencia(tmp_path):
    import signal
    cursos = [_c(KIW, "kiwify-principal", "kiwify")]
    perfil = tmp_path / "aula" / ".chrome-profile-kiwify-principal"
    perfil.mkdir(parents=True)
    sinais = []
    ex = _exec(tmp_path, cursos, zelo_grace_s=60,
               sinal_fn=lambda pid, sig, grupo: sinais.append((pid, sig, grupo)),
               comando_fn=lambda pid: f"/Applications/Chrome --user-data-dir={perfil} --x")
    z = _zel(tmp_path, cursos)
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0)
    chrome = ex.sp.mundo.novo()
    (perfil / "SingletonLock").symlink_to(f"host-{chrome.pid}")
    ex.sp.terminar(0, 0, "viva")
    _passo(z, ex, T0 + 60)
    assert sinais == [(chrome.pid, signal.SIGTERM, False)]
    _passo(z, ex, T0 + 100)
    assert len(sinais) == 1                                    # a carência começa a contar
    _passo(z, ex, T0 + 200)
    assert sinais[-1] == (chrome.pid, signal.SIGKILL, False)   # o navegador, sem grupo


def test_rollback_flag_desligada_o_ciclo_ainda_vigia_o_zelo_orfao(tmp_path):
    """ATHENA_ZELADOR_ATIVO=0 + restart no meio de um zelo: não há zelador (None), mas o
    ciclo roda o vigia persistido — o zelo pendurado não segura a conta para sempre."""
    import signal
    ex2, proc, sinais = _zelo_e_restart(tmp_path)
    athena_local.ciclo_local([_c(KIW, "kiwify-principal", "kiwify")], ex2, lambda c: (10, 10),
                             _Voz(), {}, {}, agora=T0 + 601, zelador=None)
    assert (proc.pid, signal.SIGTERM, False) in sinais


def test_o_zelo_recebe_o_teto_proprio_do_motor_no_env(tmp_path):
    cursos = [_c(KIW, "kiwify-principal", "kiwify")]
    ex = _exec(tmp_path, cursos, zelo_timeout_s=600)
    ex.disparar_zelo("kiwify:kiwify-principal", cursos[0], agora=T0)
    env = ex.sp.calls[0]["env"]
    assert float(env["ATHENA_ZELADOR_TETO_S"]) < 600          # termina antes do SIGTERM
    assert float(env["ATHENA_ZELADOR_TETO_S"]) == 480
    lk = _lock(ex, "kiwify-principal")
    assert lk["ts"] == T0 and lk["perfil"].endswith(".chrome-profile-kiwify-principal")


# ==========================================================================================
# REVISÃO DO P7 — item 4: a sonda escolhe um curso que a captura PODE rodar
# ==========================================================================================
HOT2 = "https://hotmart.com/pt-br/club/x/products/2"
HOT3 = "https://hotmart.com/pt-br/club/x/products/3"


def test_sonda_nao_usa_curso_travado_benchado_nem_fora_do_gate(tmp_path):
    cursos = [_c(HOT, "hotmart-principal", "hotmart"), _c(HOT2, "hotmart-principal", "hotmart"),
              _c(HOT3, "hotmart-principal", "hotmart"),
              _c("https://hotmart.com/pt-br/club/x/products/4", "hotmart-principal", "hotmart")]
    estado = {HOT: {"irredutivel": True, "ultima_causa": "escalar_reseed",   # 403 de 1 produto
                    "_morte_ciclo": T0 - H},
              HOT2: {"irredutivel": True, "benched_exit5": True, "ultima_causa": "relancar"},
              HOT3: {"_saida_limpa_ciclo": T0 - 2 * DIA},
              "https://hotmart.com/pt-br/club/x/products/4": {"_saida_limpa_ciclo": T0 - DIA}}
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    _ocioso(ex, "hotmart-principal", T0 - DIA)
    _passo(z, ex, T0, estado=estado,
           ativos=frozenset({HOT, HOT2, HOT3}))                # o 4º ficou fora do gate
    assert ex.sp.zelos()[0]["cmd"][4] == HOT3


def test_sonda_prefere_o_curso_de_captura_limpa_mais_recente(tmp_path):
    cursos = [_c(HOT, "hotmart-principal", "hotmart"), _c(HOT2, "hotmart-principal", "hotmart"),
              _c(HOT3, "hotmart-principal", "hotmart")]
    estado = {HOT2: {"_saida_limpa_ciclo": T0 - DIA}, HOT3: {"_saida_limpa_ciclo": T0 - 3 * DIA}}
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    _ocioso(ex, "hotmart-principal", T0 - DIA)
    _passo(z, ex, T0, estado=estado)
    assert ex.sp.zelos()[0]["cmd"][4] == HOT2


def test_conta_inteira_travada_por_reseed_ainda_e_sondada_para_o_M4(tmp_path):
    cursos = [_c(HOT, "hotmart-principal", "hotmart"), _c(HOT2, "hotmart-principal", "hotmart")]
    estado = {HOT: {"irredutivel": True, "benched_exit5": True},
              HOT2: {"irredutivel": True, "ultima_causa": "escalar_reseed", "_morte_ciclo": T0}}
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    _ocioso(ex, "hotmart-principal", T0 - DIA)
    _passo(z, ex, T0, estado=estado)
    assert ex.sp.zelos()[0]["cmd"][4] == HOT2


def test_nada_sondavel_nao_zela_e_diz_por_que(tmp_path):
    cursos = [_c(HOT, "hotmart-principal", "hotmart")]
    estado = {HOT: {"irredutivel": True, "benched_exit5": True, "ultima_causa": "relancar"}}
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    _ocioso(ex, "hotmart-principal", T0 - DIA)
    _passo(z, ex, T0, estado=estado)
    assert ex.sp.zelos() == []
    assert _status(tmp_path)["contas"][0]["decisao"] == "sem-curso-sondavel"


# ==========================================================================================
# REVISÃO DO P7 — item 5: o comando exato do alerta roda também para a Stoa
# ==========================================================================================
def test_comando_do_alerta_da_stoa_leva_a_arvore_dela(tmp_path):
    cursos = [_c(STOA, "stoa-principal", "stoa"), _c(KIW, "kiwify-principal", "kiwify")]
    ex, z, al = _exec(tmp_path, cursos), _zel(tmp_path, cursos), FakeAlertas()
    for c in ("stoa-principal", "kiwify-principal"):
        _ocioso(ex, c, T0 - DIA)
    for st in z.estado.values():
        st["status"] = "aguardando-humano"
    _passo(z, ex, T0, alertas=al)
    (alvos, comando), = al.logins
    assert alvos == ("kiwify:kiwify-principal", "stoa:stoa-principal")
    assert comando == (f"ATHENA_MOTOR_DIR_STOA={tmp_path / 'stoa'} uv run scripts/reseed.py "
                       "kiwify:kiwify-principal stoa:stoa-principal")


def test_comando_reseed_so_prefixa_com_stoa_e_protege_o_caminho():
    assert zmod.comando_reseed(["kiwify:k"], motor_dir_stoa="/x/stoa") == \
        "uv run scripts/reseed.py kiwify:k"
    assert zmod.comando_reseed(["stoa:stoa-principal"], motor_dir_stoa="/a b/stoa") == \
        "ATHENA_MOTOR_DIR_STOA='/a b/stoa' uv run scripts/reseed.py stoa:stoa-principal"
    assert zmod.comando_reseed(["stoa:stoa-principal"]) == \
        "uv run scripts/reseed.py stoa:stoa-principal"


def test_alerta_preventivo_da_stoa_tambem_leva_a_arvore(tmp_path):
    cursos = [_c(STOA, "stoa-principal", "stoa")]
    ex, z, al = _exec(tmp_path, cursos), _zel(tmp_path, cursos), FakeAlertas()
    _ocioso(ex, "stoa-principal", T0 - DIA)
    st = z.estado["stoa:stoa-principal"]
    st["status"], st["relogio"] = "viva", "duro"
    st["_t"]["expira_ts"] = T0 + DIA
    _passo(z, ex, T0, alertas=al)
    assert al.vence and al.vence[0][3].startswith(f"ATHENA_MOTOR_DIR_STOA={tmp_path / 'stoa'} ")


# ==========================================================================================
# REVISÃO DO P7 — item 6: cada camada de defesa com o seu dente
# ==========================================================================================
def test_zelo_em_curso_vale_pela_memoria_mesmo_sem_o_lock_em_disco(tmp_path):
    """Camada 1 do 1-por-vez: o zelo que ESTA encarnação spawnou conta mesmo que o lock
    dele tenha sumido do disco (apagado por fora) — senão um 2º navegador de zelo subiria."""
    cursos = [_c(KIW, "kiwify-principal", "kiwify"), _c(CUR, "curseduca-segueadi", "curseduca")]
    ex = _exec(tmp_path, cursos)
    ex.disparar_zelo("kiwify:kiwify-principal", cursos[0], agora=T0)
    os.remove(ex._lock_path("kiwify-principal"))
    assert ex.zelo_em_curso() is True
    with pytest.raises(captura.ContaOcupada):
        ex.disparar_zelo("curseduca:curseduca-segueadi", cursos[1], agora=T0)
    assert len(ex.sp.calls) == 1


def test_disparar_zelo_com_lock_de_captura_diz_a_verdade_no_motivo(tmp_path):
    """Camada `contas_livres`: com a CAPTURA da conta rodando (lock + Chrome dela no perfil),
    o motivo é o lock — não "navegador vivo sem lock de ninguém" (o log não mente)."""
    cursos = [_c(KIW, "kiwify-principal", "kiwify")]
    ex = _exec(tmp_path, cursos)
    perfil = tmp_path / "aula" / ".chrome-profile-kiwify-principal"
    perfil.mkdir(parents=True)
    ex.disparar(KIW)
    captura_proc = ex.sp.calls[0]["proc"]
    (perfil / "SingletonLock").symlink_to(f"host-{captura_proc.pid}")
    with pytest.raises(captura.ContaOcupada) as exc:
        ex.disparar_zelo("kiwify:kiwify-principal", cursos[0], agora=T0)
    assert "com lock" in str(exc.value) and "sem lock de ninguém" not in str(exc.value)
    assert len(ex.sp.calls) == 1


def test_o_lock_do_proprio_zelo_nao_conta_como_atividade_de_captura(tmp_path):
    cursos = [_c(KIW, "kiwify-principal", "kiwify")]
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0)
    assert ex.captura_viva("kiwify-principal") is False
    _passo(z, ex, T0 + 120)                                    # zelo ainda rodando
    st = z.estado["kiwify:kiwify-principal"]
    assert st["_t"]["ultima_captura_ts"] == pytest.approx(T0 - DIA)
    ex.sp.terminar(0, 0, "viva")
    _passo(z, ex, T0 + 180)
    ex.disparar(KIW)                                           # a captura, ao contrário, conta
    assert ex.captura_viva("kiwify-principal") is True


def test_curso_benchado_nunca_e_sondado_nem_como_ultimo_recurso(tmp_path):
    """Defesa em profundidade: o bench de exit-5 marca a URL que o motor não abre. Mesmo
    que o estado também diga "latch de reseed", a sonda não a usa."""
    cursos = [_c(HOT, "hotmart-principal", "hotmart")]
    estado = {HOT: {"irredutivel": True, "benched_exit5": True,
                    "ultima_causa": "escalar_reseed"}}
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    _ocioso(ex, "hotmart-principal", T0 - DIA)
    _passo(z, ex, T0, estado=estado)
    assert ex.sp.zelos() == []


# ==========================================================================================
# RODADA 10 — conta aguardando-humano: o zelo que não provou viva NUNCA dispara o próximo
# (achado bloqueante da rodada 8); o gatilho é o CONTEÚDO do arquivo de sessão, não mtime
# ==========================================================================================
def _aguardando_kiwify(tmp_path):
    """Kiwify morta PROVADA no 1º zelo (T0): conta aguardando-humano, perfil e arquivo reais
    no disco (o dublê mexe na raiz do perfil em TODA abertura do navegador)."""
    sess = tmp_path / "aula" / ".kiwify-session.json"
    sess.parent.mkdir(parents=True, exist_ok=True)
    sess.write_text("{}")
    perfil = tmp_path / "aula" / ".chrome-profile-kiwify-principal"
    perfil.mkdir(parents=True, exist_ok=True)
    for p in (sess, perfil):
        os.utime(p, (T0 - 2 * DIA, T0 - 2 * DIA))
    cursos = [_c(KIW, "kiwify-principal", "kiwify", str(sess))]
    ex, z, al = _exec(tmp_path, cursos), _zel(tmp_path, cursos), FakeAlertas()
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0, alertas=al)
    ex.sp.terminar(0, 3, "morta")
    _passo(z, ex, T0 + 60, alertas=al)
    assert z.estado["kiwify:kiwify-principal"]["status"] == "aguardando-humano"
    return sess, perfil, ex, z, al


@pytest.mark.parametrize("code,resultado,detalhe", [
    (5, "inconclusiva", "SessionProbeInconclusiveError"),     # queda de rede / 5xx
    (5, "inconclusiva", "teto de tempo"),                     # teto próprio do motor
    (-15, None, ""),                                          # watchdog: TERM, sem linha
    (5, "inconclusiva", "sem renovacao do app na sessao"),    # a Alpaclass sem o r5
    (5, "ocupado", "perfil em uso"),                          # um --reseed avulso no perfil
])
def test_aguardando_zelo_que_nao_concluiu_nunca_dispara_o_proximo_na_hora_mas_o_gatilho_fica(
        tmp_path, code, resultado, detalhe):
    """O cenário da rodada 8 (reproduzido contra 020ca1a: 4 zelos em 6 min, até o teto por
    hora): o arquivo foi reescrito por fora, a re-sonda cai num desfecho que NÃO concluiu — e
    o Chrome dela mexeu no perfil. Nada disso dispara o próximo NA HORA. REESCRITO na rodada
    11: a versão anterior exigia "nunca mais" — o bug do achado bloqueante (a reescrita foi um
    login de verdade; a conta ficava aguardando para sempre). A re-sonda segue PENDENTE e sai
    depois do espaçamento; só um desfecho conclusivo (aqui, morta) a consome."""
    sess, perfil, ex, z, al = _aguardando_kiwify(tmp_path)
    sess.write_text('{"cookies": ["relogin-avulso"]}')       # alguém mexeu DE VERDADE
    t = T0 + 2 * H
    _passo(z, ex, t, alertas=al)
    assert len(ex.sp.zelos()) == 2
    ex.sp.terminar(1, code, resultado, detalhe=detalhe)       # o dublê mexe no perfil (+30 s)
    for k in range(1, 14):                                    # 26 min de ciclos de 2 min
        _passo(z, ex, t + 120 * k, alertas=al)
    assert len(ex.sp.zelos()) == 2, z._decisao                # o laço da rodada 8 fechado
    assert z._decisao["kiwify:kiwify-principal"] == "aguardando-espacamento"
    _passo(z, ex, t + 13 * H, alertas=al)                     # passado o espaçamento
    assert len(ex.sp.zelos()) == 3, ("o login nunca mais foi provado", z._decisao)
    ex.sp.terminar(2, 3, "morta")                             # conclusivo: consome o gatilho
    for dt in (13 * H + 120, 14 * H, 2 * DIA, 4 * DIA):
        _passo(z, ex, t + dt, alertas=al)
    assert len(ex.sp.zelos()) == 3, z._decisao
    assert z.estado["kiwify:kiwify-principal"]["status"] == "aguardando-humano"
    assert z._decisao["kiwify:kiwify-principal"] == "aguardando-humano"
    assert len(al.logins) == 1


def test_aguardando_mtime_do_perfil_ou_do_arquivo_nunca_resonda(tmp_path):
    """O Chrome (do zelo ou de quem for) mexe na raiz do perfil; um `touch`/backup mexe no
    mtime do arquivo: NENHUM é ação humana nem prova de sessão viva."""
    sess, perfil, ex, z, al = _aguardando_kiwify(tmp_path)
    for dt in (H, 6 * H, DIA, 2 * DIA):
        os.utime(perfil, (T0 + dt, T0 + dt))
        os.utime(sess, (T0 + dt, T0 + dt))
        _passo(z, ex, T0 + dt + 60, alertas=al)
    assert len(ex.sp.zelos()) == 1


def test_aguardando_o_que_o_zelo_regravou_e_absorvido_na_morte(tmp_path):
    """Defesa em profundidade: se um motor um dia regravar o arquivo num zelo que termina em
    MORTE (conclusivo), a referência passa a ser a identidade de DEPOIS dele — o que o
    próprio zelo gravou nunca dispara o próximo."""
    sess, perfil, ex, z, al = _aguardando_kiwify(tmp_path)
    sess.write_text('{"cookies": ["relogin-avulso"]}')
    _passo(z, ex, T0 + 2 * H, alertas=al)
    assert len(ex.sp.zelos()) == 2
    sess.write_text('{"cookies": ["o proprio zelo regravou"]}')
    ex.sp.terminar(1, 3, "morta")
    for dt in (2 * H + 60, 4 * H, 8 * H, 2 * DIA, 5 * DIA):
        _passo(z, ex, T0 + dt, alertas=al)
    assert len(ex.sp.zelos()) == 2


def test_aguardando_inconclusivos_seguidos_resondam_so_sob_o_backoff(tmp_path):
    """REESCRITO na rodada 11 (a versão anterior exigia que a ação humana fosse descartada
    num inconclusivo): o gatilho segue pendente enquanto nada conclui, mas as re-sondas
    saem ESPAÇADAS pelo backoff (1 h, 2 h, 4 h... até o intervalo) — mesmo que o próprio
    zelo regrave o arquivo. Nunca uma por ciclo, nunca o teto por hora."""
    sess, perfil, ex, z, al = _aguardando_kiwify(tmp_path)
    sess.write_text('{"cookies": ["relogin-avulso"]}')
    t = T0 + 2 * H
    fim = t + 3 * DIA
    while t < fim:
        _passo(z, ex, t, alertas=al)
        n = len(ex.sp.zelos())
        if ex.sp.calls[n - 1]["proc"].poll() is None:
            sess.write_text(json.dumps({"cookies": [f"o proprio zelo regravou {t}"]}))
            ex.sp.terminar(n - 1, 5, "inconclusiva", detalhe="SessionProbeInconclusiveError")
        t += 600
    ts = [c["t"] for c in ex.sp.zelos()[1:]]
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    assert len(ts) >= 3, ts                                   # segue pendente: re-sonda
    assert all(g >= H for g in gaps), gaps                    # nunca na hora
    assert gaps == sorted(gaps), gaps                         # backoff crescente
    intervalo = z.estado["kiwify:kiwify-principal"]["_t"]["intervalo_s"]
    # teto: o intervalo (+ 1 ciclo até a colheita + 1 ciclo de granulação do passo)
    assert max(gaps) <= intervalo + 1200, (gaps, intervalo)
    assert len(al.logins) == 1


def test_aguardando_reescritas_seguidas_resondam_espacado(tmp_path):
    """O arquivo mudando a cada 10 min (a captura de outro curso re-persistindo) com o zelo
    ainda lendo morte: as re-sondas vêm espaçadas (30 min, 1 h, 2 h...), nunca uma por
    mudança — o teto de navegação numa conta dada como morta é o espaçamento, não o teto/h."""
    sess, perfil, ex, z, al = _aguardando_kiwify(tmp_path)
    t = T0 + 60
    while t < T0 + 3 * H:
        t += 600
        sess.write_text(json.dumps({"cookies": [f"reescrita-{t}"]}))
        _passo(z, ex, t, alertas=al)
        n = len(ex.sp.zelos())
        if ex.sp.calls[n - 1]["proc"].poll() is None:
            ex.sp.terminar(n - 1, 3, "morta")
    assert len(ex.sp.zelos()) == 3, [c["t"] - T0 for c in ex.sp.zelos()]   # T0, ~30 min, ~1h30


def test_aguardando_carimbo_humano_passa_na_frente_do_espacamento(tmp_path):
    sess, perfil, ex, z, al = _aguardando_kiwify(tmp_path)
    _carimbar(ex, "kiwify-principal", T0 + 300)
    _passo(z, ex, T0 + 360, alertas=al)                       # bem dentro dos 30 min
    assert len(ex.sp.zelos()) == 2


# ==========================================================================================
# RODADA 10 — a URL da sonda não depende de estado que some no restart
# ==========================================================================================
def test_url_que_provou_viva_e_persistida_e_vence_depois_do_restart(tmp_path):
    """O estado da captura (limpa recente, travado, benchado) mora em memória e some num
    restart: a sonda voltava ao 1º curso do cadastro (no Hotmart, um produto sem acesso = 403
    = 'login necessário' imediato). A URL que PROVOU viva vai para o status e vence depois."""
    cursos = [_c(HOT, "hotmart-principal", "hotmart"), _c(HOT2, "hotmart-principal", "hotmart"),
              _c(HOT3, "hotmart-principal", "hotmart")]
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    _ocioso(ex, "hotmart-principal", T0 - DIA)
    _passo(z, ex, T0, estado={HOT3: {"_saida_limpa_ciclo": T0 - DIA}})
    assert ex.sp.zelos()[0]["cmd"][4] == HOT3
    ex.sp.terminar(0, 0, "viva")
    _passo(z, ex, T0 + 60, estado={HOT3: {"_saida_limpa_ciclo": T0 - DIA}})
    assert _status(tmp_path)["contas"][0]["url_sonda"] == HOT3
    z2 = _zel(tmp_path, cursos)                                # restart: estado da captura vazio
    _passo(z2, ex, T0 + 13 * H, estado={})
    assert len(ex.sp.zelos()) == 2 and ex.sp.zelos()[1]["cmd"][4] == HOT3
    # travado NESTA encarnação, a provada sai da escolha (o estado em memória só EXCLUI)
    ex.sp.terminar(1, 0, "viva")
    _passo(z2, ex, T0 + 13 * H + 60, estado={})
    z3 = _zel(tmp_path, cursos)
    _passo(z3, ex, T0 + 26 * H, estado={HOT3: {"irredutivel": True, "benched_exit5": True}})
    assert ex.sp.zelos()[2]["cmd"][4] == HOT


def test_url_sonda_do_status_so_vale_se_for_da_unidade(tmp_path):
    cursos = [_c(HOT, "hotmart-principal", "hotmart"), _c(HOT2, "hotmart-principal", "hotmart")]
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    _passo(z, ex, T0 - 2 * DIA)                                # grava um status
    dados = _status(tmp_path)
    dados["contas"][0]["url_sonda"] = "https://evil.exemplo/products/9"
    (tmp_path / "sessoes-status.json").write_text(json.dumps(dados))
    z2 = _zel(tmp_path, cursos)
    assert "url_sonda" not in z2.estado["hotmart:hotmart-principal"]


# ==========================================================================================
# RODADA 10 — lacuna de dente: o vigia PERSISTIDO não toca no zelo DESTA encarnação
# ==========================================================================================
def test_vigia_persistido_ignora_o_zelo_desta_encarnacao(tmp_path):
    """O guarda `nossos.get(conta) == dados` é o único que separa o vigia persistido (zelos
    de encarnações anteriores) do zelo que ESTA encarnação vigia em memória. Sem ele: TERM em
    dobro e o lock regravado (a remoção só-se-for-o-nosso do fim do zelo falharia)."""
    cursos = [_c(KIW, "kiwify-principal", "kiwify")]
    sinais = []
    box = {}
    ex = _exec(tmp_path, cursos, zelo_timeout_s=600, zelo_grace_s=60,
               sinal_fn=lambda pid, sig, grupo: sinais.append((pid, sig, grupo)),
               comando_fn=lambda pid: _CMD_ZELO.format(KIW) if pid == box.get("pid") else None)
    z = _zel(tmp_path, cursos)
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0)
    box["pid"] = ex.sp.calls[0]["proc"].pid
    antes = _lock(ex, "kiwify-principal")
    assert ex.vigiar_zelos_orfaos(T0 + 700) == []             # além do teto, com prova
    assert sinais == []
    assert _lock(ex, "kiwify-principal") == antes


@pytest.mark.parametrize("detalhe", ["sem renovacao do app na sessao"])
def test_frases_novas_da_alpaclass_chegam_ao_status_e_nao_sao_frase_de_morte(tmp_path, detalhe):
    """A frase fixa do motor/zelador.py da Alpaclass (rodada 11: a Alpaclass sem a renovação do
    app dentro do ensure_session não é zelada) passa pela lista branca do status (senão virava
    'detalhe descartado' e o operador não saberia POR QUE o zelo foi inconclusivo) e não casa
    a assinatura de morte/credencial do daemon. As da rodada 10 saíram com a renovação própria
    do zelador (o motor não as emite mais)."""
    from maestro import causa
    assert not causa._RE_SESSAO.search(detalhe) and not causa._RE_TOKEN.search(detalhe)
    ALPA = "https://fsp.alpaclass.com/"
    cursos = [_c(ALPA, "alpaclass-principal", "alpaclass")]
    ex, z = _exec(tmp_path, cursos), _zel(tmp_path, cursos)
    _ocioso(ex, "alpaclass-principal", T0 - DIA)
    _passo(z, ex, T0)
    ex.sp.terminar(0, 5, "inconclusiva", detalhe=detalhe)
    _passo(z, ex, T0 + 60)
    conta, = _status(tmp_path)["contas"]
    assert conta["resultado"] == "inconclusiva" and conta["detalhe"] == detalhe
    assert conta["status"] != "aguardando-humano"


def test_backoff_com_contador_enorme_nao_estoura_o_float(tmp_path):
    """Meses de inconclusivos/mortes seguidos: 2**(n-1) acima de 2**1023 não cabe num float e
    o `_aplicar` estouraria (OverflowError) a cada zelo — o espaçamento sumiria."""
    for resultado, code, campo in (("inconclusiva", 5, "inconclusivas_seguidas"),
                                   ("morta", 3, "mortes_seguidas")):
        d = tmp_path / resultado
        d.mkdir()
        cursos = [_c(KIW, "kiwify-principal", "kiwify")]
        ex, z = _exec(d, cursos), _zel(d, cursos)
        _ocioso(ex, "kiwify-principal", T0 - DIA)
        st = z.estado["kiwify:kiwify-principal"]
        st[campo] = 5000
        if resultado == "morta":
            st["status"] = "aguardando-humano"
            st["_t"]["ident_na_morte"] = 1
            _carimbar(ex, "kiwify-principal", T0 - 60)
        _passo(z, ex, T0)
        ex.sp.terminar(0, code, resultado)
        _passo(z, ex, T0 + 60)
        st = z.estado["kiwify:kiwify-principal"]
        assert st[campo] == 5001, (resultado, st[campo])      # o _aplicar foi até o fim
        assert st["_t"]["proximo_ts"] == T0 + 60 + st["_t"]["intervalo_s"], resultado


# ==========================================================================================
# RODADA 11 (o fecho do P7) — o GATILHO HUMANO só é consumido por desfecho CONCLUSIVO
# (achado bloqueante da revisão da rodada 10: o carimbo era consumido no DISPARO e a
# identidade relida depois de QUALQUER zelo — um blip no zelo urgente perdia o login humano)
# ==========================================================================================
@pytest.mark.parametrize("code,resultado,detalhe", [
    (5, "inconclusiva", "SessionProbeInconclusiveError"),     # rede / 5xx
    (5, "inconclusiva", "teto de tempo"),                     # teto próprio do motor
    (-15, None, ""),                                          # watchdog: TERM, sem linha
])
def test_login_humano_seguido_de_inconclusivo_segue_pendente_sob_backoff_e_viva_rearma(
        tmp_path, code, resultado, detalhe):
    """O cenário do revisor (falhava 3/3 contra f5f2c76): conta aguardando com a captura
    travada; o humano roda o reseed.py (arquivo reescrito + carimbo); o zelo urgente cai num
    blip. O login NÃO se perde: a nova tentativa espera o backoff (nada na hora) e, viva,
    rearma a captura."""
    sess, _, cursos, estado = _latch_kiwify(tmp_path, T0 - 10 * H, T0 - 2 * DIA)
    ex, z, al = _exec(tmp_path, cursos), _zel(tmp_path, cursos), FakeAlertas()
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0, estado=estado, alertas=al)
    ex.sp.terminar(0, 3, "morta")
    _passo(z, ex, T0 + 60, estado=estado, alertas=al)
    assert z.estado["kiwify:kiwify-principal"]["status"] == "aguardando-humano"
    sess.write_text('{"cookies": ["login-humano"]}')
    _carimbar(ex, "kiwify-principal", T0 + 3 * H)
    t = T0 + 3 * H + 60
    _passo(z, ex, t, estado=estado, alertas=al)
    assert len(ex.sp.zelos()) == 2                            # urgente, na hora
    ex.sp.terminar(1, code, resultado, detalhe=detalhe)       # o blip
    for k in range(1, 26):                                    # 50 min de ciclos de 2 min
        _passo(z, ex, t + 120 * k, estado=estado, alertas=al)
    assert len(ex.sp.zelos()) == 2, z._decisao                # nunca na hora (rodada 8)
    assert z._decisao["kiwify:kiwify-principal"] == "aguardando-espacamento"
    _passo(z, ex, t + 2 * H, estado=estado, alertas=al)       # passado o backoff de 1 h
    assert len(ex.sp.zelos()) == 3, ("o login humano nunca mais foi provado", z._decisao)
    ex.sp.terminar(2, 0, "viva")
    _passo(z, ex, t + 2 * H + 60, estado=estado, alertas=al)
    assert z.estado["kiwify:kiwify-principal"]["status"] == "viva"
    assert "irredutivel" not in estado[KIW]                  # M4: a captura rearmou
    for dt in (3 * H, 4 * H):                                 # e o carimbo foi consumido
        _passo(z, ex, t + dt, estado=estado, alertas=al)
    assert len(ex.sp.zelos()) == 3


def test_login_humano_nao_consumido_pelo_blip_sobrevive_ao_restart(tmp_path):
    """O `carimbo_visto` é persistido: se o blip o consumisse, nem um restart traria o zelo
    de volta (o achado). Agora o restart acha o carimbo PENDENTE (o `carimbo_tentado` e o
    backoff também persistidos) e o prova depois do espaçamento."""
    sess, _, cursos, estado = _latch_kiwify(tmp_path, T0 - 10 * H, T0 - 2 * DIA)
    ex, z, al = _exec(tmp_path, cursos), _zel(tmp_path, cursos), FakeAlertas()
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0, estado=estado, alertas=al)
    ex.sp.terminar(0, 3, "morta")
    _passo(z, ex, T0 + 60, estado=estado, alertas=al)
    sess.write_text('{"cookies": ["login-humano"]}')
    _carimbar(ex, "kiwify-principal", T0 + 3 * H)
    _passo(z, ex, T0 + 3 * H + 60, estado=estado, alertas=al)
    ex.sp.terminar(1, 5, "inconclusiva", detalhe="SessionProbeInconclusiveError")
    _passo(z, ex, T0 + 3 * H + 120, estado=estado, alertas=al)
    t = _status(tmp_path)["contas"][0]["_t"]
    assert t["carimbo_tentado"] == T0 + 3 * H
    assert (t.get("carimbo_visto") or 0) < T0 + 3 * H         # PENDENTE no disco
    z2 = _zel(tmp_path, cursos)                               # restart do daemon
    _passo(z2, ex, T0 + 3 * H + 600, estado=estado, alertas=al)
    assert len(ex.sp.zelos()) == 2                            # o backoff sobreviveu também
    _passo(z2, ex, T0 + 5 * H, estado=estado, alertas=al)
    assert len(ex.sp.zelos()) == 3
    ex.sp.terminar(2, 0, "viva")
    _passo(z2, ex, T0 + 5 * H + 60, estado=estado, alertas=al)
    assert "irredutivel" not in estado[KIW]


def test_carimbo_mais_novo_fura_o_backoff_do_carimbo_ja_tentado(tmp_path):
    """O humano agiu DE NOVO (outro reseed.py) enquanto o 1º carimbo esperava o backoff: o
    carimbo novo é outra ação humana e passa na frente, como sempre passou."""
    sess, _, cursos, estado = _latch_kiwify(tmp_path, T0 - 10 * H, T0 - 2 * DIA)
    ex, z, al = _exec(tmp_path, cursos), _zel(tmp_path, cursos), FakeAlertas()
    _ocioso(ex, "kiwify-principal", T0 - DIA)
    _passo(z, ex, T0, estado=estado, alertas=al)
    ex.sp.terminar(0, 3, "morta")
    _passo(z, ex, T0 + 60, estado=estado, alertas=al)
    _carimbar(ex, "kiwify-principal", T0 + 3 * H)
    _passo(z, ex, T0 + 3 * H + 60, estado=estado, alertas=al)
    ex.sp.terminar(1, 5, "inconclusiva", detalhe="SessionProbeInconclusiveError")
    _passo(z, ex, T0 + 3 * H + 120, estado=estado, alertas=al)
    _passo(z, ex, T0 + 3 * H + 300, estado=estado, alertas=al)
    assert len(ex.sp.zelos()) == 2                            # o 1º espera o backoff
    _carimbar(ex, "kiwify-principal", T0 + 3 * H + 400)       # 2º reseed humano
    _passo(z, ex, T0 + 3 * H + 460, estado=estado, alertas=al)
    assert len(ex.sp.zelos()) == 3                            # na hora


def test_login_humano_com_captura_de_outro_curso_ativa_nao_se_perde_no_blip(tmp_path):
    """Conta NÃO aguardando (o zelador não viu a morte): um curso travou (latch), OUTRO curso
    da conta segue capturando (conta nunca ociosa). O humano relogou (carimbo) e o zelo
    urgente caiu num blip. Se o blip consumisse o carimbo, o zelo seguinte esperaria a conta
    ficar ociosa — o que não acontece com a outra captura rodando — e o curso travado nunca
    rearmaria. Pendente, ele volta depois do backoff, sem esperar a ociosidade."""
    sess, _, cursos, estado = _latch_kiwify(tmp_path, T0 - H, T0 - 2 * DIA)
    KIW3 = "https://dashboard.kiwify.com.br/courses/terceiro"
    cursos = cursos + [_c(KIW3, "kiwify-principal", "kiwify", str(sess))]
    ex, z, al = _exec(tmp_path, cursos), _zel(tmp_path, cursos), FakeAlertas()
    _carimbar(ex, "kiwify-principal", T0 - 600)               # o humano relogou há 10 min
    t = T0
    _ocioso(ex, "kiwify-principal", t - 60)                   # KIW3 capturando agora
    _passo(z, ex, t, estado=estado, alertas=al)
    assert len(ex.sp.zelos()) == 1                            # urgente, apesar da captura
    ex.sp.terminar(0, 5, "inconclusiva", detalhe="SessionProbeInconclusiveError")
    while t < T0 + 3 * H:
        t += 600
        _ocioso(ex, "kiwify-principal", t - 60)               # a outra captura não para
        _passo(z, ex, t, estado=estado, alertas=al)
        n = len(ex.sp.zelos())
        if n == 2 and ex.sp.calls[-1]["proc"].poll() is None:
            ex.sp.terminar(len(ex.sp.calls) - 1, 0, "viva")
    zelos = ex.sp.zelos()
    assert len(zelos) == 2, ("o login humano nunca mais foi provado", z._decisao)
    assert zelos[1]["t"] - zelos[0]["t"] >= H                 # depois do backoff
    assert "irredutivel" not in estado[KIW]                  # e a captura travada rearmou


def test_carimbo_e_consumido_pela_morte_provada(tmp_path):
    """O outro desfecho conclusivo: o humano relogou, o zelo PROVOU morte (o login não
    pegou). O carimbo foi respondido — não gera outro zelo (a conta volta ao aguardando e
    ao alerta, sem martelar)."""
    sess, perfil, ex, z, al = _aguardando_kiwify(tmp_path)
    _carimbar(ex, "kiwify-principal", T0 + H)
    _passo(z, ex, T0 + H + 60, alertas=al)
    assert len(ex.sp.zelos()) == 2
    ex.sp.terminar(1, 3, "morta")
    for dt in (H + 120, 3 * H, 13 * H, 2 * DIA, 5 * DIA):
        _passo(z, ex, T0 + dt, alertas=al)
    assert len(ex.sp.zelos()) == 2, z._decisao
    assert z._decisao["kiwify:kiwify-principal"] == "aguardando-humano"


# --- lacunas de dente da revisão da rodada 10 (código correto; agora com observável) -------
def test_aguardando_sem_referencia_de_identidade_nao_resonda_a_atual_vira_a_base(tmp_path):
    """Status de uma versão sem `ident_na_morte` (ou referência perdida): a conta aguardando
    NÃO é re-sondada por isso — sem base, a identidade atual vira a base. Só uma mudança
    DEPOIS dela re-sonda."""
    sess, perfil, ex, z, al = _aguardando_kiwify(tmp_path)
    dados = _status(tmp_path)
    dados["contas"][0]["_t"].pop("ident_na_morte")
    (tmp_path / "sessoes-status.json").write_text(json.dumps(dados))
    sess.write_text('{"cookies": ["mexido antes da referencia"]}')
    z2 = _zel(tmp_path, [_c(KIW, "kiwify-principal", "kiwify", str(sess))])   # restart
    for dt in (2 * H, 6 * H, DIA, 2 * DIA):
        _passo(z2, ex, T0 + dt, alertas=al)
    assert len(ex.sp.zelos()) == 1, z2._decisao
    assert z2._decisao["kiwify:kiwify-principal"] == "aguardando-humano"
    sess.write_text('{"cookies": ["login depois da referencia"]}')
    _passo(z2, ex, T0 + 3 * DIA, alertas=al)
    assert len(ex.sp.zelos()) == 2


@pytest.mark.parametrize("some", ["apagado", "ilegivel"])
def test_aguardando_arquivo_de_sessao_sumido_ou_ilegivel_nao_resonda(tmp_path, some):
    """Identidade 0 (arquivo ausente/ilegível) nunca é "mudou": o motor sairia 5 sem navegar
    (sem-sessao-salva), mas o zelador não spawna nada à toa numa conta dada como morta."""
    sess, perfil, ex, z, al = _aguardando_kiwify(tmp_path)
    if some == "apagado":
        sess.unlink()
    else:
        sess.unlink()
        sess.mkdir()                                          # open() -> IsADirectoryError
    for dt in (2 * H, 6 * H, DIA, 3 * DIA):
        _passo(z, ex, T0 + dt, alertas=al)
    assert len(ex.sp.zelos()) == 1, z._decisao
    assert z._decisao["kiwify:kiwify-principal"] == "aguardando-humano"


def _lock_exclusivo_de_fora(ex, conta, dados):
    """O que `motor/conta_lock.py` (o reseed.py) faz: temp COMPLETO + os.link (exclusivo)."""
    path = ex._lock_path(conta)
    tmp = path + ".de-fora.tmp"
    with open(tmp, "w") as f:
        json.dump(dados, f)
    try:
        os.link(tmp, path)
    finally:
        os.remove(tmp)


@pytest.mark.parametrize("quem", ["a-conta", "a-parceira"])
def test_reseed_que_pega_o_lock_na_janela_do_disparar_zelo_vence_sem_sobrescrita(tmp_path,
                                                                                   quem):
    """Lacuna (a) da rodada 10: entre o `contas_livres` ("sem lock") e a criação do lock do
    zelo cabem o `_montar` e o `ps` do `_navegador_no_perfil`. O reseed.py (ou a sonda p102)
    cria o lock NESSA janela — reproduzida DE VERDADE: ele entra de dentro do `comando_fn`
    (o `ps` do PID do SingletonLock, aqui um PID reusado por outro processo). Com uma escrita
    simples no lugar da criação EXCLUSIVA, o zelo sobrescreveria o lock do humano, limparia
    o SingletonLock e abriria um 2º navegador na conta (ban)."""
    sess = str(tmp_path / "aula" / ".memberkit-session.json")
    cursos = [_c(MK_T, "memberkit-triade", "memberkit", sess),
              _c(MK_E, "memberkit-empreender", "memberkit", sess)]
    alvo = "memberkit-triade" if quem == "a-conta" else "memberkit-empreender"
    externo = {"pid": 4343, "course_url": "reseed:memberkit", "conta": alvo,
               "ts": T0, "dono": "reseed"}
    box = {}

    def ps(pid):                                  # a janela: o reseed entra AQUI
        _lock_exclusivo_de_fora(box["ex"], alvo, externo)
        return "/usr/sbin/outro-processo"          # PID reusado: não é o navegador do perfil

    ex = _exec(tmp_path, cursos, comando_fn=ps)
    box["ex"] = ex
    ex.sp.mundo.procs[4242] = FakeProc()           # o PID do SingletonLock está VIVO
    perfil = tmp_path / "aula" / ".chrome-profile-memberkit-triade"
    perfil.mkdir(parents=True)
    (perfil / "SingletonLock").symlink_to("host-4242")
    with pytest.raises(captura.ContaOcupada):
        ex.disparar_zelo("memberkit:memberkit-triade", cursos[0],
                         parceiras=["memberkit-empreender"], agora=T0)
    assert ex.sp.calls == []                                  # nenhum navegador
    assert _lock(ex, alvo) == externo                         # o lock do humano INTACTO
    outra = ({"memberkit-triade", "memberkit-empreender"} - {alvo}).pop()
    assert not os.path.lexists(ex._lock_path(outra))          # o nosso foi desfeito
    assert os.path.lexists(perfil / "SingletonLock")          # o perfil não foi mexido
    assert sorted(os.listdir(tmp_path / "locks")) == [os.path.basename(ex._lock_path(alvo))]
