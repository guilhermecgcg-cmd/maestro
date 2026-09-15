"""RODADA 2, ITEM 7 — o bloqueio do YouTube abre uma SUSPENSÃO GLOBAL dos passes do YouTube.

INCIDENTE REAL (15/09, provado entre 11:04 e 11:13 — decisoes/2026-09-15.jsonl e as autópsias
20260915T110725, T111035 e T111348 de hotmart-principal): as 15 aulas Hotmart do Ciro Gestor
foram para o passe youtube (p102). O daemon disparou `motor.cli <url> --youtube` às 11:04:35; o
motor abortou com exit 4 tipo youtube ("Sign in to confirm you're not a bot" — o IP do Mac
barrado); o daemon escalou às 11:07:25 e REDISPAROU às 11:07:50, e de novo 11:10:36 → 11:10:59.
O aborto do YouTube não entra na escada e vira retentativa em minutos. Cada retentativa abre
páginas na conta Hotmart paga e bate de novo no YouTube, aprofundando o bloqueio.

AGORA:
  - um exit 4 da classe `youtube` (linha de máquina ou frase do YouTube, sobre a saída inteira)
    abre a SUSPENSÃO GLOBAL, persistida no mesmo arquivo de estado: 3 h na primeira vez,
    dobrando a cada bloqueio novo dentro de 48 h do anterior (3 → 6 → 12 → 24 h, teto 24 h);
  - enquanto ela vale, NENHUM curso dispara o passe `--youtube`; os passes que não tocam o
    YouTube seguem normais;
  - quando a janela vence, sai UM disparo de passe youtube (a prova); os outros esperam o
    resultado — sucesso ou aborto de outra classe encerra a suspensão, novo bloqueio reabre no
    degrau seguinte;
  - o aborto do YouTube não conta na série de bench do curso;
  - log e voz dizem "YouTube bloqueou o IP do Mac — passes do YouTube suspensos até HH:MM
    (degrau N)", uma vez por abertura.

Dublês com dentes: o `LocalExecutor` REAL (escolha de passe, lock, reap, cópia da cauda do .err)
com spawn de mentira; os módulos REAIS do ciclo (disjuntor/vigia/causa). Nada de processo real.
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from maestro import athena_local, causa, disjuntor, vigia
from maestro.adaptadores import captura
from tests.test_antiban_relancamento_exit4 import _Alertas, _sem_tracker_vivo  # noqa: F401
from tests.test_athena_integracao import FakeVoz
from tests.test_executor_local import DIR, PY, FakeSpawn

H = 3600.0
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T_DISPARO = time.mktime((2026, 9, 15, 11, 4, 35, 0, 0, -1))          # 15/09 11:04:35
T_ABORTO = time.mktime((2026, 9, 15, 11, 8, 0, 0, 0, -1))            # 11:08:00
T_REDISPARO = time.mktime((2026, 9, 15, 11, 10, 59, 0, 0, -1))       # 11:10:59

CIRO = "https://hotmart.com/pt-br/club/ciro-gestor/products/5431484"
OUTRO = "https://hotmart.com/pt-br/club/outro-curso/products/2222222"
SEM_YT = "https://hotmart.com/pt-br/club/sem-youtube/products/3333333"
QUARTO = "https://hotmart.com/pt-br/club/quarto-curso/products/4444444"
_CONTAS = {CIRO: "hotmart-principal", OUTRO: "hotmart-outra", SEM_YT: "hotmart-terceira",
           QUARTO: "hotmart-quarta"}

# a cauda REAL das autópsias de 15/09 (o CLI do motor 2cf9d04 no `SystemExit(4)` do YouTube)
_ABORTO_YOUTUBE = (
    "2026-09-15 11:07:20,512 WARNING motor.youtube: ERROR: [youtube] kJQP7kiw5Fk: Sign in to "
    "confirm you're not a bot. Use --cookies-from-browser or --cookies for the authentication.\n"
    "2026-09-15 11:07:24,101 ERROR motor.orchestrator: RUN ABORTADO com 0/15 aulas concluídas: "
    "o caminho daqui até o YouTube está barrado: verificação anti-bot, limite de taxa, \"Your IP "
    "is likely being blocked\", recusa 403 dos fragmentos, formato indisponível ou cliente não "
    "suportado (yt-dlp desatualizado). NÃO é a plataforma do curso e NÃO é a conta: é o caminho "
    "até o YouTube (IP residencial/cliente/rede). Parando com ok=0 audio=0 falhou=1 de 15.\n"
    "\n⛔ PASSE --youtube PARADO POR BLOQUEIO DO YOUTUBE: o caminho daqui até o YouTube está "
    "barrado. NÃO é a plataforma do curso e NÃO é a conta: é o caminho até o YouTube (IP "
    "residencial/cliente/rede). Parando com ok=0 audio=0 falhou=1 de 15.\n"
    "\nO QUE FAZER (a Hotmart não está envolvida):\n"
    "1. ATUALIZE O YT-DLP no venv do motor.\n"
    "2. Se já está atualizado: espere ~1 hora.\n")
# D3 (contrato com o motor M4): a prova que capturou diz `YOUTUBE_PROVA resultado=ok` ao sair.
# Reescrito, não ajustado: sem a linha, a saída limpa da prova ENCERRAVA a suspensão — é
# exatamente a regra que o contrato substitui (sem a linha a suspensão continua).
_PROVA_CAPTUROU = "Stats: total=15 ok=15 audio=0 falhou=0\nYOUTUBE_PROVA resultado=ok\n"


_MUNDOS = []                                                         # tabelas de processo dos _Mac


@pytest.fixture(autouse=True)
def _passe_youtube_ligado(monkeypatch):
    monkeypatch.setenv("ATHENA_YOUTUBE_ATIVO", "1")                   # como no launch.sh vivo
    # O VIGIA consulta a MESMA tabela de processos do executor: com o `os.kill(pid, 0)` real, o
    # PID de mentira de um motor ainda rodando (4000+) parecia morto (ou, pior, colidia com um
    # processo real desta máquina) — a varredura de locks via uma "órfã" que não existe.
    _MUNDOS.clear()
    monkeypatch.setattr(vigia, "_pid_vivo", lambda pid: any(m.vivo(pid) for m in _MUNDOS))


class _Mac:
    """O Mac do dono: o LocalExecutor REAL (spawn de mentira, lock e log num temporário) e o
    ciclo REAL do daemon. `pend` é a fila de cada curso por passe (mutável no teste)."""

    def __init__(self, tmp_path, pend):
        self.tmp = tmp_path
        self.pend = pend
        self.sp = FakeSpawn()
        _MUNDOS.append(self.sp.mundo)
        self.cursos = [captura.CursoLocal(url=u, conta=c, plataforma="hotmart",
                                          total_esperado=100) for u, c in _CONTAS.items()]
        self.ex = captura.LocalExecutor(
            self.cursos, motor_python=PY, motor_dir=DIR, spawn=self.sp,
            lock_dir=str(tmp_path / "locks"), pid_vivo=self.sp.mundo.vivo,
            motor_log_dir=str(tmp_path / "logs"), pendencias_fn=self._pendencias)
        self.estado, self.voo, self.voz, self.alertas = {}, {}, FakeVoz(), _Alertas()

    def _pendencias(self, url, motor_dir):
        p = {"base": 0, "audio": 0, "embed": 0, "nao-video": 0, "youtube": 0}
        p.update(self.pend.get(url, {}))
        return p

    def kw(self):
        return dict(disjuntor=disjuntor, vigia=vigia, causa=causa, alertas=self.alertas,
                    lock_dir=str(self.tmp / "locks"), autopsia_dir=str(self.tmp / "aut"),
                    boot_ts=1.0)

    def ciclo(self, agora, cursos):
        lista = [c for c in self.cursos if c.url in cursos]
        athena_local.ciclo_local(lista, self.ex, lambda c: (10, 100), self.voz, self.voo,
                                 self.estado, agora=agora, **self.kw())

    def youtube(self, url=None):
        return [c for c in self.sp.calls
                if "--youtube" in c["cmd"] and (url is None or c["cmd"][3] == url)]

    def comandos(self, url):
        return [c["cmd"] for c in self.sp.calls if c["cmd"][3] == url]

    def terminar(self, url, codigo, saida):
        call = [c for c in self.sp.calls if c["cmd"][3] == url][-1]
        path = self.ex._stderr_path(_CONTAS[url])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(saida)
        call["proc"].encerrar(codigo)

    def avisos(self):
        return [str(t) for _p, t in self.voz.escaladas if "YouTube bloqueou o IP do Mac" in str(t)]


def _hhmm(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def _bloqueio_inicial(mac):
    """O incidente: dispara o passe youtube do Ciro Gestor às 11:04:35 e ele aborta às 11:08."""
    mac.ciclo(T_DISPARO, [CIRO])
    assert len(mac.youtube(CIRO)) == 1
    mac.terminar(CIRO, 4, _ABORTO_YOUTUBE)
    mac.ciclo(T_ABORTO + 60, [CIRO])                                  # autópsia (11:09)
    return T_ABORTO + 60


def test_a_cauda_do_incidente_e_exit4_para_humano_e_classe_youtube():
    obito = SimpleNamespace(curso=CIRO, conta="hotmart-principal", exit_code=4,
                            stderr_tail=_ABORTO_YOUTUBE)
    assert causa.classificar(obito, plataforma="hotmart").acao == "escalar_humano"


# ==========================================================================
# 1) o incidente reproduzido
# ==========================================================================
def test_o_incidente_de_15_09_nao_redispara_passe_youtube_depois_do_bloqueio(tmp_path):
    mac = _Mac(tmp_path, {CIRO: {"youtube": 15}})
    t_aut = _bloqueio_inicial(mac)
    mac.pend[OUTRO] = {"youtube": 8}                                  # outra conta, fila do YouTube
    mac.ciclo(T_REDISPARO, [CIRO, OUTRO])                             # 11:10:59
    assert mac.youtube(OUTRO) == [], "outro curso bateu no YouTube com o IP do Mac barrado"
    mac.ciclo(T_ABORTO + 20 * 60, [CIRO, OUTRO])                      # 11:28: a espera de 600 s venceu
    assert len(mac.youtube(CIRO)) == 1, "o Ciro Gestor voltou ao YouTube minutos depois do bloqueio"
    assert mac.youtube(OUTRO) == []
    [aviso] = mac.avisos()                                            # UMA vez por abertura
    assert aviso == (f"YouTube bloqueou o IP do Mac — passes do YouTube suspensos até "
                     f"{_hhmm(t_aut + 3 * H)} (degrau 1)"), aviso


def test_o_mesmo_bloqueio_visto_por_dois_cursos_abre_a_suspensao_uma_vez_so(tmp_path):
    # duas contas com passe do YouTube no ar batem no MESMO bloqueio do IP: uma abertura, um
    # aviso, degrau 1 — o segundo abort não pode subir o degrau (a janela já está aberta)
    mac = _Mac(tmp_path, {CIRO: {"youtube": 15}, OUTRO: {"youtube": 8}})
    mac.ciclo(T_DISPARO, [CIRO, OUTRO])
    assert len(mac.youtube()) == 2
    mac.terminar(CIRO, 4, _ABORTO_YOUTUBE)
    mac.terminar(OUTRO, 4, _ABORTO_YOUTUBE)
    mac.ciclo(T_ABORTO + 60, [CIRO, OUTRO])
    [aviso] = mac.avisos()
    assert "(degrau 1)" in aviso, aviso


def test_curso_hotmart_sem_youtube_segue_disparando_durante_a_suspensao(tmp_path):
    mac = _Mac(tmp_path, {CIRO: {"youtube": 15}})
    _bloqueio_inicial(mac)
    mac.pend[SEM_YT] = {"base": 3}
    mac.pend[OUTRO] = {"youtube": 4, "audio": 2}
    mac.ciclo(T_REDISPARO, [CIRO, OUTRO, SEM_YT])
    assert mac.comandos(SEM_YT) == [[PY, "-m", "motor.cli", SEM_YT]]
    assert mac.comandos(OUTRO) == [[PY, "-m", "motor.cli", OUTRO, "--audio"]]


def test_so_youtube_na_fila_nao_vira_sonda_base_durante_a_suspensao(tmp_path):
    # sem isto o executor caía no "nada elegível -> sonda base": abria a conta à toa
    mac = _Mac(tmp_path, {CIRO: {"youtube": 15}})
    _bloqueio_inicial(mac)
    mac.pend[OUTRO] = {"youtube": 8}
    mac.ciclo(T_REDISPARO, [OUTRO])
    assert mac.comandos(OUTRO) == []


# ==========================================================================
# 2) a janela sobrevive ao reinício; a leitura é tolerante
# ==========================================================================
class _Relogio:
    def __init__(self, t):
        self.t = float(t)
        self.time = lambda: self.t
        self.strftime = time.strftime
        self.localtime = time.localtime


def _rodar(mac, cursos, rel, *, path, iters, apos_ciclo=None):
    async def _dormir(s):
        rel.t += s
        if apos_ciclo is not None:
            apos_ciclo()

    asyncio.run(athena_local.rodar(
        [c for c in mac.cursos if c.url in cursos], mac.ex, lambda c: (10, 100), mac.voz,
        sleep=_dormir, intervalo_s=180.0, max_iters=iters, estado_cursos_path=path,
        **mac.kw()))


def test_a_suspensao_sobrevive_ao_reinicio_do_daemon(tmp_path, monkeypatch):
    path = str(tmp_path / "estado_cursos.json")
    rel = _Relogio(T_DISPARO)
    monkeypatch.setattr(athena_local, "time", rel)
    mac1 = _Mac(tmp_path, {CIRO: {"youtube": 15}})
    terminou = []

    def _aborta_uma_vez():
        if mac1.youtube(CIRO) and not terminou:
            mac1.terminar(CIRO, 4, _ABORTO_YOUTUBE)
            terminou.append(True)

    _rodar(mac1, [CIRO], rel, path=path, iters=2, apos_ciclo=_aborta_uma_vez)
    assert len(mac1.youtube(CIRO)) == 1 and mac1.avisos()
    rel.t += 20 * 60                                                  # processo NOVO, 20 min depois
    mac2 = _Mac(tmp_path, {CIRO: {"youtube": 15}, OUTRO: {"youtube": 8}})
    _rodar(mac2, [OUTRO], rel, path=path, iters=1)
    assert mac2.youtube(OUTRO) == [], "o reinício esqueceu o bloqueio do YouTube"


@pytest.mark.parametrize("youtube", ["lixo", 3, {"ate": "amanhã", "degrau": -2},
                                     {"ate": 1e18, "degrau": 1}, None])
def test_estado_do_youtube_ilegivel_ou_antigo_carrega_sem_erro_e_nao_suspende(tmp_path,
                                                                            monkeypatch,
                                                                            youtube):
    path = str(tmp_path / "estado_cursos.json")
    dados = {"versao": 1, "cursos": {}}
    if youtube is not None:
        dados["youtube"] = youtube
    with open(path, "w") as f:
        json.dump(dados, f)
    rel = _Relogio(T_DISPARO)
    monkeypatch.setattr(athena_local, "time", rel)
    mac = _Mac(tmp_path, {CIRO: {"youtube": 15}})
    _rodar(mac, [CIRO], rel, path=path, iters=1)
    assert len(mac.youtube(CIRO)) == 1


# ==========================================================================
# 3) a liberação: UM disparo, os outros esperam o resultado
# ==========================================================================
def test_quando_a_janela_vence_sai_UM_passe_youtube_e_os_outros_esperam_o_resultado(tmp_path):
    mac = _Mac(tmp_path, {CIRO: {"youtube": 15}, OUTRO: {"youtube": 8}, QUARTO: {"youtube": 3}})
    t_aut = _bloqueio_inicial(mac)
    todos = [CIRO, OUTRO, QUARTO]                                     # três contas com fila do YouTube
    t = t_aut + 3 * H + 60                                            # a janela venceu
    mac.ciclo(t, todos)
    assert len(mac.youtube()) == 2, "a liberação soltou mais de um passe do YouTube"
    prova = mac.youtube()[-1]["cmd"][3]
    mac.ciclo(t + 180, todos)
    assert len(mac.youtube()) == 2, "outro passe do YouTube saiu antes do resultado da prova"
    mac.terminar(prova, 0, _PROVA_CAPTUROU)                           # a prova capturou
    mac.ciclo(t + 360, todos)
    # a suspensão ACABOU: os dois que esperavam saem juntos (senão cada passe do YouTube dali
    # em diante virava uma prova nova, um por vez, para sempre)
    assert len(mac.youtube()) == 4, "o sucesso da prova não encerrou a suspensão"


# ==========================================================================
# 4) a janela dobra, para no teto, o degrau zera; o curso não é benchado
# ==========================================================================
def _degraus(avisos):
    return [int(re.search(r"\(degrau (\d+)\)", a).group(1)) for a in avisos]


def test_a_janela_dobra_a_cada_bloqueio_e_para_no_teto_de_24h(tmp_path):
    mac = _Mac(tmp_path, {CIRO: {"youtube": 15}})
    t = T_DISPARO
    fins = []
    for i, janela_h in enumerate((3, 6, 12, 24, 24)):
        mac.ciclo(t, [CIRO])                                          # a prova de cada janela
        assert len(mac.youtube(CIRO)) == i + 1, (i, "a prova não saiu quando a janela venceu")
        mac.terminar(CIRO, 4, _ABORTO_YOUTUBE)
        mac.ciclo(t + 60, [CIRO])                                     # bloqueio de novo
        fins.append(t + 60 + janela_h * H)
        mac.ciclo(t + 60 + janela_h * H - 120, [CIRO])                # ainda dentro: nada sai
        assert len(mac.youtube(CIRO)) == i + 1, (i, "saiu passe do YouTube dentro da janela")
        t = t + 60 + janela_h * H + 60
    avisos = mac.avisos()
    assert _degraus(avisos) == [1, 2, 3, 4, 4], avisos
    assert [a.split("até ")[1][:5] for a in avisos] == [_hhmm(f) for f in fins], avisos
    # 5 bloqueios do YouTube seguidos e o curso NÃO foi benchado: não é culpa dele
    assert not mac.estado[CIRO].get("benched_exit4") and not mac.estado[CIRO].get("exit4_seguidas")


def test_o_degrau_zera_depois_de_48h_sem_bloqueio(tmp_path):
    mac = _Mac(tmp_path, {CIRO: {"youtube": 15}})
    t_aut = _bloqueio_inicial(mac)                                    # degrau 1 (3 h)
    t = t_aut + 3 * H + 60
    mac.ciclo(t, [CIRO])                                              # prova
    mac.terminar(CIRO, 4, _ABORTO_YOUTUBE)
    mac.ciclo(t + 60, [CIRO])                                         # degrau 2 (6 h)
    t2 = t + 60 + 6 * H + 60
    mac.ciclo(t2, [CIRO])                                             # prova
    mac.terminar(CIRO, 0, _PROVA_CAPTUROU)                            # captura: suspensão acaba
    mac.ciclo(t2 + 60, [CIRO])
    t3 = t + 60 + 49 * H                                              # 49 h depois do último bloqueio
    mac.ciclo(t3, [CIRO])
    assert len(mac.youtube(CIRO)) == 4
    mac.terminar(CIRO, 4, _ABORTO_YOUTUBE)
    mac.ciclo(t3 + 60, [CIRO])
    assert _degraus(mac.avisos()) == [1, 2, 1], mac.avisos()


# ==========================================================================
# 5) as envs novas: teto no limite de leitura do estado
# ==========================================================================
def test_o_teto_da_suspensao_nao_passa_do_limite_de_leitura_do_estado():
    r = subprocess.run(
        [sys.executable, "-c", "from maestro import athena_local as a; "
         "print(a._YOUTUBE_SUSPENSAO_S, a._YOUTUBE_SUSPENSAO_TETO_S)"],
        cwd=RAIZ, env=dict(os.environ, ATHENA_YOUTUBE_SUSPENSAO_S="999999",
                           ATHENA_YOUTUBE_SUSPENSAO_TETO_S="432000"),
        capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-1200:]
    assert r.stdout.split() == ["172800.0", "172800.0"], (r.stdout, r.stderr[-600:])
