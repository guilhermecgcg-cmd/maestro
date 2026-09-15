"""D3 — a PROVA da suspensão global do YouTube, no contrato com o motor M4 (aula 8e4023f).

O MOTOR M4 (motor/youtube_suspensao.py) lê o arquivo de estado do daemon A CADA pedido ao
YouTube: com `ate` presente (vencido ou não) ou uma prova em curso, NENHUM processo pede nada ao
YouTube — exceto o que traz `ATHENA_YOUTUBE_PROVA == str(prova_desde)` lido NA HORA. Ao sair ele
imprime no STDERR (atexit) `YOUTUBE_PROVA resultado=ok|bloqueado|nao_tocou`.

O QUE ESTAVA ERRADO NO DAEMON (a619bd6):
  - a prova só era marcada DEPOIS do spawn (conf `:passe=youtube`) e o estado só ia ao disco no
    fim do ciclo: o motor subia sem a prova no arquivo e se tratava como suspenso — nenhuma
    prova de verdade acontecia; e o token nem existia;
  - só o `--youtube` do Hotmart levava a prova; o `base` das plataformas com player YouTube não;
  - achado 1 da revisão independente: a prova que morria com -9 ou exit 5 ENCERRAVA a suspensão
    (a regra era "classe != youtube encerra") e soltava os passes de 3 contas de uma vez;
  - nada avisava de uma suspensão vencida sem passe elegível para a prova;
  - item 7: um carregador cujo trabalho era todo YouTube suspenso saía 0 sem avanço e virava
    "curso quiescido" (cooldown de 6 h); e a espera virava "captura ESTAGNADA" na vigília.

Dublês: o `LocalExecutor` REAL com spawn de mentira que registra o arquivo de estado NO INSTANTE do
spawn; e um teste com PROCESSO REAL (o tee do .err até a saída bruta da autópsia).
"""
import inspect
import json
import os
import sys
import textwrap
import time

import pytest

from maestro import athena_local, causa, disjuntor, vigia
from maestro.adaptadores import captura
from tests.test_antiban_r2_suspensao_youtube import (  # noqa: F401 (fixture autouse)
    _ABORTO_YOUTUBE, _MUNDOS, CIRO, OUTRO, QUARTO, H, T_ABORTO, T_DISPARO,
    _passe_youtube_ligado)
from tests.test_antiban_relancamento_exit4 import _Alertas, _sem_tracker_vivo  # noqa: F401
from tests.test_athena_integracao import FakeVoz
from tests.test_executor_local import DIR, PY, FakeSpawn

MK = "https://tenant-yt.memberkit.com.br/curso-a"
GR = "https://sierramkt.greenn.club/"
KW = "https://dashboard.kiwify.com.br/courses"
_CURSOS = {CIRO: ("hotmart-principal", "hotmart"), OUTRO: ("hotmart-outra", "hotmart"),
           QUARTO: ("hotmart-quarta", "hotmart"), MK: ("memberkit-yt", "memberkit"),
           GR: ("greenn-principal", "greenn"), KW: ("kiwify-principal", "kiwify")}
ENV_PROVA = "ATHENA_YOUTUBE_PROVA"
AVISO_VENCIDA = "suspensão do YouTube vencida sem prova — nenhum passe elegível"
_STATS_OK = "Stats: total=15 ok=15 audio=0 falhou=0\n"


def _linha(resultado):
    return f"YOUTUBE_PROVA resultado={resultado}\n"


def _aceita(fn, nome):
    """O código sob teste aceita o parâmetro? (compatível com o código de ANTES, para o vermelho
    ser pelo comportamento e não por TypeError)."""
    return nome in inspect.signature(fn).parameters


class _SpawnQueLeOEstado(FakeSpawn):
    """Spawn de mentira que lê o arquivo de estado NO INSTANTE do spawn — o que o motor M4 leria
    ao pedir o primeiro vídeo."""

    def __init__(self, path):
        super().__init__()
        self.path = path
        self.falhar = False

    def __call__(self, cmd, *, env, cwd):
        if self.falhar:
            raise OSError("spawn falhou (dublê)")
        proc = super().__call__(cmd, env=env, cwd=cwd)
        try:
            with open(self.path) as f:
                dados = json.load(f)
        except Exception:
            dados = None
        self.calls[-1]["estado_no_spawn"] = dados
        return proc


class _Mac3:
    def __init__(self, tmp_path, pend):
        self.tmp = tmp_path
        self.pend = pend
        self.path = str(tmp_path / "estado_cursos.json")
        self.sp = _SpawnQueLeOEstado(self.path)
        _MUNDOS.append(self.sp.mundo)
        self.cursos = [captura.CursoLocal(url=u, conta=c, plataforma=p, total_esperado=100)
                       for u, (c, p) in _CURSOS.items()]
        kw = dict(motor_python=PY, motor_dir=DIR, spawn=self.sp, lock_dir=str(tmp_path / "locks"),
                  pid_vivo=self.sp.mundo.vivo, motor_log_dir=str(tmp_path / "logs"),
                  pendencias_fn=self._pendencias)
        if _aceita(captura.LocalExecutor.__init__, "estado_cursos_path"):
            kw["estado_cursos_path"] = self.path
        self.ex = captura.LocalExecutor(self.cursos, **kw)
        self.estado, self.voo, self.voz, self.alertas = {}, {}, FakeVoz(), _Alertas()
        self.falhar_gravacao = False

    def _pendencias(self, url, motor_dir):
        p = {"base": 0, "audio": 0, "embed": 0, "nao-video": 0, "youtube": 0}
        p.update(self.pend.get(url, {}))
        return p

    def ciclo(self, agora, cursos):
        lista = [c for c in self.cursos if c.url in cursos]
        kw = dict(disjuntor=disjuntor, vigia=vigia, causa=causa, alertas=self.alertas,
                  lock_dir=str(self.tmp / "locks"), autopsia_dir=str(self.tmp / "aut"), boot_ts=1.0)
        if _aceita(athena_local.ciclo_local, "persistir_estado"):
            kw["persistir_estado"] = ((lambda: False) if self.falhar_gravacao else
                                      (lambda: athena_local._gravar_estado_cursos(self.path,
                                                                                 self.estado)))
        athena_local.ciclo_local(lista, self.ex, lambda c: (10, 100), self.voz, self.voo,
                                 self.estado, agora=agora, **kw)
        athena_local._gravar_estado_cursos(self.path, self.estado)       # o fim de ciclo do rodar

    def chamadas(self, url=None):
        return [c for c in self.sp.calls if url is None or c["cmd"][3] == url]

    def com_token(self):
        return [c for c in self.sp.calls if ENV_PROVA in c["env"]]

    def youtube(self, url=None):
        return [c for c in self.chamadas(url) if "--youtube" in c["cmd"]]

    def terminar(self, url, codigo, saida):
        call = self.chamadas(url)[-1]
        path = self.ex._stderr_path(_CURSOS[url][0])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(saida)
        call["proc"].encerrar(codigo)

    def disco(self):
        with open(self.path) as f:
            return json.load(f).get("youtube", {})

    def avisos(self, trecho):
        return [str(t) for _p, t in self.voz.escaladas if trecho in str(t)]


def _abrir_suspensao(mac):
    """O Ciro Gestor bate no bloqueio do YouTube: janela de 3 h aberta no ciclo da autópsia."""
    mac.ciclo(T_DISPARO, [CIRO])
    mac.terminar(CIRO, 4, _ABORTO_YOUTUBE)
    mac.ciclo(T_ABORTO + 60, [CIRO])
    assert mac.disco().get("ate") == T_ABORTO + 60 + 3 * H, mac.disco()
    return T_ABORTO + 60


# ==========================================================================
# 1) o portador e o token
# ==========================================================================
def test_a_prova_vai_no_passe_youtube_com_o_token_ja_gravado_no_disco_antes_do_spawn(tmp_path):
    mac = _Mac3(tmp_path, {CIRO: {"youtube": 15}})
    t_aut = _abrir_suspensao(mac)
    mac.ciclo(t_aut + 3 * H + 60, [CIRO])                  # a janela venceu
    call = mac.chamadas(CIRO)[-1]
    assert "--youtube" in call["cmd"]
    token = call["env"].get(ENV_PROVA)
    assert token is not None, "o disparo que vence a janela não levou o token da prova"
    no_spawn = (call["estado_no_spawn"] or {}).get("youtube", {})
    assert "prova_desde" in no_spawn, "o motor subiria sem a prova gravada no arquivo"
    assert token == str(float(no_spawn["prova_desde"]))    # a comparação do motor
    disco = mac.disco()                                    # ida e volta pelo JSON
    assert token == str(float(disco["prova_desde"])) and disco["prova_curso"] == CIRO
    assert call["env"].get("ATHENA_ESTADO_CURSOS_PATH") == mac.path


@pytest.mark.parametrize("url,carrega", [(MK, True), (GR, True), (KW, False)],
                         ids=["memberkit", "greenn", "kiwify-nao-carrega"])
def test_o_base_das_plataformas_com_player_youtube_carrega_a_prova(tmp_path, url, carrega):
    mac = _Mac3(tmp_path, {CIRO: {"youtube": 15}, url: {"base": 3}})
    t_aut = _abrir_suspensao(mac)
    mac.ciclo(t_aut + 3 * H + 60, [url])
    call = mac.chamadas(url)[-1]
    assert (ENV_PROVA in call["env"]) is carrega, call["cmd"]
    assert (mac.disco().get("prova_curso") == url) is carrega, mac.disco()


def test_uma_prova_so_por_janela_mesmo_com_tres_portadores_no_ciclo(tmp_path):
    mac = _Mac3(tmp_path, {CIRO: {"youtube": 15}, MK: {"base": 3}, GR: {"base": 2}})
    t_aut = _abrir_suspensao(mac)
    mac.ciclo(t_aut + 3 * H + 60, [CIRO, MK, GR])
    assert len(mac.com_token()) == 1, [c["cmd"] for c in mac.com_token()]


def test_sem_prova_nenhum_disparo_leva_token_nem_o_herdado_do_ambiente(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_PROVA, "1789000000.0")          # vazou no ambiente do daemon
    mac = _Mac3(tmp_path, {CIRO: {"youtube": 15}, MK: {"base": 3}})
    mac.ciclo(T_DISPARO, [CIRO, MK])                       # sem suspensão
    assert mac.com_token() == []
    mac.terminar(CIRO, 4, _ABORTO_YOUTUBE)
    mac.terminar(MK, 0, "Memberkit: total=3 ok=3 audio=0 falhou=0\n")
    mac.ciclo(T_ABORTO + 60, [CIRO, MK])                   # abre a janela
    mac.ciclo(T_ABORTO + 3600, [MK])                       # dentro da janela
    assert mac.com_token() == []


# ==========================================================================
# 2) o resultado da prova
# ==========================================================================
def _prova_do_ciro(mac):
    t_aut = _abrir_suspensao(mac)
    t = t_aut + 3 * H + 60
    mac.ciclo(t, [CIRO])
    assert ENV_PROVA in mac.chamadas(CIRO)[-1]["env"]
    return t


def test_prova_ok_encerra_a_suspensao(tmp_path):
    mac = _Mac3(tmp_path, {CIRO: {"youtube": 15}, OUTRO: {"youtube": 8}, QUARTO: {"youtube": 3}})
    t = _prova_do_ciro(mac)
    mac.terminar(CIRO, 0, _STATS_OK + _linha("ok"))
    mac.ciclo(t + 180, [CIRO, OUTRO, QUARTO])
    assert "ate" not in mac.disco() and "prova_curso" not in mac.disco(), mac.disco()
    assert len(mac.youtube(OUTRO)) == 1 and len(mac.youtube(QUARTO)) == 1
    assert [c for c in mac.youtube(OUTRO) + mac.youtube(QUARTO) if ENV_PROVA in c["env"]] == []


def test_prova_bloqueada_reabre_um_degrau_acima(tmp_path):
    mac = _Mac3(tmp_path, {CIRO: {"youtube": 15}})
    t = _prova_do_ciro(mac)
    mac.terminar(CIRO, 4, _ABORTO_YOUTUBE + _linha("bloqueado"))
    mac.ciclo(t + 180, [CIRO])
    disco = mac.disco()
    assert disco.get("degrau") == 2 and disco.get("ate") == t + 180 + 6 * H, disco
    assert "prova_curso" not in disco
    assert any("(degrau 2)" in a for a in mac.avisos("YouTube bloqueou o IP do Mac"))


def test_prova_bloqueada_sobe_o_degrau_mesmo_mais_de_48h_depois_do_ultimo_bloqueio(tmp_path):
    # a suspensão nunca deixou de valer: 2 dias sem portador elegível não são "48 h sem
    # bloqueio" — a prova que bate na parede sobe o degrau, não volta ao 1
    mac = _Mac3(tmp_path, {CIRO: {"youtube": 15}})
    t_aut = _abrir_suspensao(mac)
    t = t_aut + 50 * H
    mac.ciclo(t, [CIRO])
    assert ENV_PROVA in mac.chamadas(CIRO)[-1]["env"]
    mac.terminar(CIRO, 4, _ABORTO_YOUTUBE + _linha("bloqueado"))
    mac.ciclo(t + 180, [CIRO])
    assert mac.disco().get("degrau") == 2, mac.disco()


def test_sem_gravar_o_disco_o_passe_youtube_nao_sai_sem_a_prova(tmp_path):
    # o motor só reconhece a prova lida no ARQUIVO: sem gravar, o token não vale nada e o
    # `--youtube` subiria para abrir a Hotmart e não pedir nada
    mac = _Mac3(tmp_path, {CIRO: {"youtube": 15}})
    t_aut = _abrir_suspensao(mac)
    antes = len(mac.chamadas(CIRO))
    mac.falhar_gravacao = True
    mac.ciclo(t_aut + 3 * H + 60, [CIRO])
    assert len(mac.chamadas(CIRO)) == antes, "o --youtube subiu sem a prova gravada"
    assert "prova_curso" not in mac.disco()


def test_spawn_que_falha_devolve_a_vaga_da_prova(tmp_path):
    mac = _Mac3(tmp_path, {CIRO: {"youtube": 15}, OUTRO: {"youtube": 8}})
    t_aut = _abrir_suspensao(mac)
    mac.sp.falhar = True
    mac.ciclo(t_aut + 3 * H + 60, [CIRO])
    assert "prova_curso" not in mac.disco(), "a prova ficou presa a um processo que não subiu"
    mac.sp.falhar = False
    mac.ciclo(t_aut + 3 * H + 240, [OUTRO])
    assert len(mac.com_token()) == 1


def test_prova_que_nao_tocou_o_youtube_mantem_a_suspensao_e_passa_a_prova_adiante(tmp_path):
    mac = _Mac3(tmp_path, {CIRO: {"youtube": 15}, OUTRO: {"youtube": 8}})
    t = _prova_do_ciro(mac)
    antes = mac.disco()
    token_1 = mac.chamadas(CIRO)[-1]["env"][ENV_PROVA]
    mac.terminar(CIRO, 0, _STATS_OK + _linha("nao_tocou"))
    mac.ciclo(t + 180, [CIRO, OUTRO])
    disco = mac.disco()
    assert disco.get("ate") == antes["ate"] and disco.get("degrau") == antes["degrau"], disco
    # a próxima prova foi no próximo disparo elegível (o OUTRO), com token NOVO
    novos = [c for c in mac.com_token() if c["env"][ENV_PROVA] != token_1]
    assert len(novos) == 1 and novos[0]["cmd"][3] == OUTRO, [c["cmd"] for c in mac.com_token()]
    assert disco.get("prova_curso") == OUTRO


@pytest.mark.parametrize("codigo,saida", [(-9, ""), (5, "SONDA DE SESSÃO INCONCLUSIVA (timeout)\n"),
                                          (0, _STATS_OK)],
                         ids=["sigkill", "exit5", "exit0-sem-linha"])
def test_prova_sem_linha_reabre_no_mesmo_degrau_e_nao_solta_as_outras_contas(tmp_path, codigo,
                                                                             saida):
    # achado 1 da revisão independente: a prova que morria com -9/exit 5 encerrava a suspensão
    # e soltava os passes de 3 contas de uma vez
    mac = _Mac3(tmp_path, {CIRO: {"youtube": 15}, OUTRO: {"youtube": 8}, QUARTO: {"youtube": 3}})
    t = _prova_do_ciro(mac)
    mac.terminar(CIRO, codigo, saida)
    mac.ciclo(t + 180, [CIRO, OUTRO, QUARTO])
    disco = mac.disco()
    assert disco.get("ate") == t + 180 + 3 * H and disco.get("degrau") == 1, disco
    assert "prova_curso" not in disco
    assert mac.youtube(OUTRO) == [] and mac.youtube(QUARTO) == [], "a prova morta soltou as contas"


def test_prova_sem_linha_mas_abortada_pelo_bloqueio_do_youtube_sobe_o_degrau(tmp_path):
    # motor ANTIGO (sem a linha): o exit 4 de classe youtube É a evidência de bloqueio — reabrir
    # no mesmo degrau voltaria ao IP barrado a cada 3 h para sempre
    mac = _Mac3(tmp_path, {CIRO: {"youtube": 15}})
    t = _prova_do_ciro(mac)
    mac.terminar(CIRO, 4, _ABORTO_YOUTUBE)
    mac.ciclo(t + 180, [CIRO])
    assert mac.disco().get("degrau") == 2, mac.disco()


def test_prova_sem_resultado_alem_do_teto_reabre_no_mesmo_degrau(tmp_path):
    mac = _Mac3(tmp_path, {CIRO: {"youtube": 15}, OUTRO: {"youtube": 8}})
    t = _prova_do_ciro(mac)                                # a prova nunca devolve resultado
    mac.ciclo(t + 25 * H, [OUTRO])
    disco = mac.disco()
    assert disco.get("ate") == t + 25 * H + 3 * H and "prova_curso" not in disco, disco
    assert mac.youtube(OUTRO) == []


def test_a_linha_da_prova_no_stderr_do_filho_chega_a_saida_bruta_da_autopsia(tmp_path):
    # PROCESSO REAL: o tee do .err (stdout+stderr no mesmo arquivo) cobre a linha que o motor
    # imprime no STDERR ao sair, depois de ~50 KB de stdout — e ela é a última coisa da saída
    raiz = tmp_path / "motor_falso"
    (raiz / "motor" / "memberkit").mkdir(parents=True)
    (raiz / "motor" / "__init__.py").write_text("")
    (raiz / "motor" / "memberkit" / "__init__.py").write_text("")
    (raiz / "motor" / "memberkit" / "__main__.py").write_text(textwrap.dedent('''
        import atexit, sys
        def _linha():
            sys.stdout.flush()
            sys.stderr.write("YOUTUBE_PROVA resultado=ok\\n")
            sys.stderr.flush()
        atexit.register(_linha)
        for i in range(600):
            print("2026-09-15 12:00:00,000 INFO httpx: HTTP Request: POST "
                  "https://api.notion.com/v1/pages 200 OK", i)
        print("Memberkit: total=1 ok=1 audio=0 falhou=0")
    '''))
    curso = captura.CursoLocal(url=MK, conta="memberkit-yt", plataforma="memberkit")
    ex = captura.LocalExecutor(
        [curso], motor_python=sys.executable, motor_dir=str(raiz),
        lock_dir=str(tmp_path / "locks"), motor_log_dir=str(tmp_path / "logs"),
        processos_fn=lambda: [(os.getpid(), os.getppid(), "python -m pytest")])
    assert ex.disparar(MK).startswith("local_iniciada:")
    ex._procs[MK].wait(timeout=60)
    obitos = vigia.autopsia(str(tmp_path / "locks"), ex.drenar_obitos(), agora=time.time(),
                            autopsia_dir=str(tmp_path / "aut"), stderr_path_de=ex._stderr_path,
                            boot_ts=1.0)
    [obito] = [o for o in obitos if o.curso == MK]
    assert obito.exit_code == 0
    assert obito.stderr_bruto.rstrip().endswith("YOUTUBE_PROVA resultado=ok"), \
        obito.stderr_bruto[-300:]
    assert len(obito.stderr_bruto) > 40000                 # a saída inteira, não as 40 linhas


# ==========================================================================
# 3) o aviso da suspensão vencida sem prova
# ==========================================================================
def test_suspensao_vencida_ha_mais_de_6h_sem_passe_elegivel_avisa_uma_vez(tmp_path):
    mac = _Mac3(tmp_path, {CIRO: {"youtube": 15}, KW: {"base": 3}})
    t_aut = _abrir_suspensao(mac)
    ate = t_aut + 3 * H
    mac.ciclo(ate + 5 * H, [KW])                            # vencida há 5 h: ainda não
    assert mac.avisos(AVISO_VENCIDA) == []
    mac.ciclo(ate + 6 * H + 120, [KW])
    assert mac.avisos(AVISO_VENCIDA) == [AVISO_VENCIDA]
    mac.ciclo(ate + 7 * H, [KW])                            # o mesmo episódio: não repete
    assert len(mac.avisos(AVISO_VENCIDA)) == 1


# ==========================================================================
# 4) item 7: "tudo suspenso por YouTube" é espera NEUTRA
# ==========================================================================
def test_portador_so_com_youtube_suspenso_espera_neutro_sem_cooldown_nem_estagnada(tmp_path):
    mac = _Mac3(tmp_path, {CIRO: {"youtube": 15}, MK: {"base": 4}})
    t_aut = _abrir_suspensao(mac)                           # janela de 3 h aberta
    mac.ciclo(t_aut + 120, [MK])                            # o Memberkit roda na janela, sem token
    assert len(mac.chamadas(MK)) == 1 and ENV_PROVA not in mac.chamadas(MK)[-1]["env"]
    mac.terminar(MK, 0, "Memberkit: total=4 ok=0 audio=0 falhou=4\n")   # tudo suspenso
    mac.ciclo(t_aut + 240, [MK])
    assert "cooldown_ate" not in mac.estado[MK], "tudo suspenso pelo YouTube virou curso quiescido"
    for k in range(1, 5):                                   # 2 h de janela
        mac.ciclo(t_aut + 240 + k * 1800, [MK])
    assert len(mac.chamadas(MK)) == 1, "redisparou um curso cujo trabalho está todo suspenso"
    assert mac.avisos("captura ESTAGNADA") == []
    mac.ciclo(t_aut + 3 * H + 60, [MK])                     # venceu: volta — e leva a prova
    assert len(mac.chamadas(MK)) == 2 and ENV_PROVA in mac.chamadas(MK)[-1]["env"]


def test_hotmart_so_com_youtube_na_janela_nao_vira_captura_estagnada(tmp_path):
    mac = _Mac3(tmp_path, {CIRO: {"youtube": 15}, OUTRO: {"youtube": 8}})
    t_aut = _abrir_suspensao(mac)
    for k in range(5):
        mac.ciclo(t_aut + 120 + k * 1800, [OUTRO])
    assert mac.chamadas(OUTRO) == []
    assert mac.avisos("captura ESTAGNADA") == []
