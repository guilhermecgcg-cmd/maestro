"""D3r2 — a prova do YouTube depois da revisão independente do D3 (NO-GO para instalar D3 + M4).

A1 + o adendo do M4 rodada 2: a linha da prova passa a ter QUATRO resultados,
`YOUTUBE_PROVA resultado=(ok|bloqueado|sem_sucesso|nao_tocou)` (no motor: bloqueado > ok >
sem_sucesso > nao_tocou). No daemon:
  - ok -> encerra; bloqueado -> degrau+1;
  - sem_sucesso (pediu ao YouTube, nada deu certo, nenhum sinal de bloqueio) -> mantém, reabre a
    janela no MESMO degrau, não solta outra prova na hora;
  - nao_tocou (nenhum pedido) -> mantém, libera a vaga, degrau intacto;
  - sem linha -> mantém e reabre no MESMO degrau;
  - um exit 4 de CLASSE youtube conta como bloqueado com qualquer linha (o `--youtube` que sai 4
    com "VIERAM COMO INDISPONÍVEIS E ZERO CAPTURAS" dizia nao_tocou e soltava a vaga: a prova ia
    de conta em conta, cada uma abrindo a Hotmart paga).

B1 — a órfã da prova (o Popen se perdeu num reinício do daemon):
  - o PID da prova vai ao estado e ao disco logo depois do spawn (`prova_pid`, lido tolerante);
  - só a morte DESSE PID decide a prova;
  - o curso da prova não é redisparado enquanto ela não tem resultado;
  - o lock de PID morto da prova não é apagado antes da autópsia: a conta fica travada e o .err
    dela intacto; se o lock sumir mesmo assim, a autópsia lê o .err da conta pelo PID da prova.

Dublês: os do D3 (`_Mac3`: o LocalExecutor REAL com spawn de mentira e o ciclo REAL do daemon). O
spawn da conta dupla trunca o .err da conta a cada disparo, como o `_spawn_popen`.
"""
import os
from types import SimpleNamespace

import pytest

from maestro import athena_local, causa, disjuntor, vigia
from maestro.adaptadores import captura
from tests.test_antiban_d3_prova_youtube import (  # noqa: F401
    CIRO, ENV_PROVA, H, MK, OUTRO, _abrir_suspensao, _linha, _Mac3, _prova_do_ciro,
    _SpawnQueLeOEstado, _STATS_OK)
from tests.test_antiban_r2_suspensao_youtube import (  # noqa: F401 (fixture autouse)
    _ABORTO_YOUTUBE, _MUNDOS, _passe_youtube_ligado)
from tests.test_antiban_relancamento_exit4 import _ABORT_EXIT4, _sem_tracker_vivo  # noqa: F401
from tests.test_executor_local import DIR, PY
from tests.test_revisao_d3_reinicio_na_prova import _PEND, _ciclo_com_morte_no_meio, _reiniciar

_NADA_DEU_CERTO = "Stats: total=15 ok=0 audio=0 falhou=15\n"


# ==========================================================================
# A1 + adendo M4r2: as quatro saídas da prova e as combinações com o exit 4
# ==========================================================================
@pytest.mark.parametrize("codigo,saida,esperado", [
    (0, _STATS_OK + _linha("ok"), "encerra"),
    (4, _ABORTO_YOUTUBE + _linha("bloqueado"), "sobe"),
    (0, _NADA_DEU_CERTO + _linha("sem_sucesso"), "reabre"),
    (0, _STATS_OK + _linha("nao_tocou"), "passa_adiante"),
    (4, _ABORTO_YOUTUBE + _linha("nao_tocou"), "sobe"),
    (4, _ABORTO_YOUTUBE + _linha("sem_sucesso"), "sobe"),
    (4, _ABORTO_YOUTUBE + _linha("ok"), "sobe"),
    (4, _ABORT_EXIT4 + _linha("nao_tocou"), "passa_adiante"),
    (4, _ABORT_EXIT4 + _linha("sem_sucesso"), "reabre"),
    (0, _linha("ok") + _NADA_DEU_CERTO + _linha("sem_sucesso"), "reabre"),
], ids=["ok", "bloqueado", "sem_sucesso", "nao_tocou", "nao_tocou+exit4-youtube",
        "sem_sucesso+exit4-youtube", "ok+exit4-youtube", "nao_tocou+exit4-parede",
        "sem_sucesso+exit4-parede", "a-ultima-linha-vale-sem_sucesso"])
def test_resultado_da_prova_nas_quatro_saidas_e_com_o_exit4(tmp_path, codigo, saida, esperado):
    mac = _Mac3(tmp_path, {CIRO: {"youtube": 15}, OUTRO: {"youtube": 8}})
    t = _prova_do_ciro(mac)
    antes = mac.disco()
    mac.terminar(CIRO, codigo, saida)
    mac.ciclo(t + 180, [CIRO, OUTRO])
    disco = mac.disco()
    if esperado == "encerra":
        assert "ate" not in disco and "prova_curso" not in disco, disco
        assert len(mac.youtube(OUTRO)) == 1 and ENV_PROVA not in mac.youtube(OUTRO)[-1]["env"]
    elif esperado == "sobe":
        assert disco.get("degrau") == 2 and disco.get("ate") == t + 180 + 6 * H, disco
        assert "prova_curso" not in disco, disco
        assert mac.youtube(OUTRO) == [], "o bloqueio soltou outra prova na hora"
    elif esperado == "reabre":
        assert disco.get("degrau") == 1 and disco.get("ate") == t + 180 + 3 * H, disco
        assert "prova_curso" not in disco, disco
        assert mac.youtube(OUTRO) == [], "a prova sem sucesso soltou outra prova na hora"
    else:                                                  # passa_adiante (nao_tocou)
        assert disco.get("degrau") == antes["degrau"] and disco.get("ate") == antes["ate"], disco
        assert disco.get("prova_curso") == OUTRO, disco
        assert ENV_PROVA in mac.youtube(OUTRO)[-1]["env"]


# ==========================================================================
# B1(a): o PID da prova no estado e no disco
# ==========================================================================
def test_o_pid_da_prova_vai_ao_disco_logo_depois_do_spawn(tmp_path):
    mac = _Mac3(tmp_path, {CIRO: {"youtube": 15}})
    t = _abrir_suspensao(mac) + 3 * H + 60
    lista = [c for c in mac.cursos if c.url == CIRO]
    # o ciclo SEM a gravação do fim de ciclo do `rodar`: um reinício logo depois do disparo lê isto
    athena_local.ciclo_local(
        lista, mac.ex, lambda c: (10, 100), mac.voz, mac.voo, mac.estado, agora=t,
        disjuntor=disjuntor, vigia=vigia, causa=causa, alertas=mac.alertas,
        lock_dir=str(tmp_path / "locks"), autopsia_dir=str(tmp_path / "aut"), boot_ts=1.0,
        persistir_estado=lambda: athena_local._gravar_estado_cursos(mac.path, mac.estado))
    call = mac.chamadas(CIRO)[-1]
    assert ENV_PROVA in call["env"]
    pid = call["proc"].pid
    assert mac.disco().get("prova_pid") == pid, mac.disco()
    relido = athena_local._carregar_estado_youtube(mac.path, agora=t + 30)
    assert relido.get("prova_pid") == pid and relido.get("prova_curso") == CIRO, relido


@pytest.mark.parametrize("valor,fica", [(4001, True), ("4001", False), (True, False), (0, False),
                                        (-3, False), (40.5, False), (None, False)])
def test_prova_pid_lido_tolerante_e_so_junto_da_prova(tmp_path, valor, fica):
    import json
    t = 1_789_500_000.0
    path = tmp_path / "estado_cursos.json"
    base = {"ate": t - 60, "degrau": 1, "prova_curso": CIRO, "prova_desde": t - 30,
            "prova_pid": valor}
    path.write_text(json.dumps({"versao": 1, "cursos": {}, "youtube": base}))
    sy = athena_local._carregar_estado_youtube(str(path), agora=t)
    assert sy.get("prova_curso") == CIRO and sy.get("prova_desde") == t - 30, sy
    assert ("prova_pid" in sy) is fica and (not fica or sy["prova_pid"] == 4001), sy
    sem_prova = dict(base, prova_pid=4001)
    del sem_prova["prova_curso"]
    path.write_text(json.dumps({"versao": 1, "cursos": {}, "youtube": sem_prova}))
    assert "prova_pid" not in athena_local._carregar_estado_youtube(str(path), agora=t)


# ==========================================================================
# B1(b): só a morte do PID da prova decide a prova
# ==========================================================================
def test_a_morte_de_outro_pid_do_mesmo_curso_nao_decide_a_prova():
    t = 1_789_500_000.0
    sy = {"ate": t - 60, "degrau": 1, "ultimo_bloqueio": t - 3 * H - 60, "prova_curso": CIRO,
          "prova_desde": t - 30, "prova_pid": 4001}
    estado = {athena_local._CHAVE_YOUTUBE: sy}
    esp = athena_local._EspinhaNula()
    outro = SimpleNamespace(exit_code=0, stderr_bruto=_STATS_OK + _linha("ok"), stderr_tail="",
                            pid=4002)
    athena_local._observar_youtube(estado, CIRO, outro, agora=t, voz=None, espinha=esp)
    assert sy.get("prova_curso") == CIRO and sy.get("ate") == t - 60, (
        f"a morte de outro PID encerrou a suspensão: {sy}")
    prova = SimpleNamespace(exit_code=4, stderr_bruto=_ABORTO_YOUTUBE + _linha("bloqueado"),
                            stderr_tail="", pid=4001)
    athena_local._observar_youtube(estado, CIRO, prova, agora=t + 60, voz=None, espinha=esp)
    assert "prova_curso" not in sy and "prova_pid" not in sy and sy.get("degrau") == 2, sy


# ==========================================================================
# B1(d): o lock da prova órfã morta segura a conta até a autópsia; sem lock, o .err pelo PID
# ==========================================================================
CIRO2 = "https://hotmart.com/pt-br/club/ciro-gestor-2/products/5431485"   # a MESMA conta do Ciro


class _SpawnQueTruncaOErr(_SpawnQueLeOEstado):
    """Como o `_spawn_popen`: cada disparo TRUNCA o .err da conta (é o que apagaria a evidência da
    prova órfã se a conta fosse redisparada antes da autópsia)."""

    def __call__(self, cmd, *, env, cwd):
        proc = super().__call__(cmd, env=env, cwd=cwd)
        err = env.get(captura._ENV_STDERR_TEE)
        if err:
            os.makedirs(os.path.dirname(err), exist_ok=True)
            open(err, "wb").close()
        return proc


class _MacContaDupla(_Mac3):
    """O Mac do D3 com um SEGUNDO curso na conta do Ciro (uma conta Hotmart tem vários cursos)."""

    def __init__(self, tmp_path, pend, *, sp=None):
        super().__init__(tmp_path, pend)
        if sp is None:
            sp = _SpawnQueTruncaOErr(self.path)
            _MUNDOS.append(sp.mundo)
        self.sp = sp
        self.cursos.append(captura.CursoLocal(url=CIRO2, conta="hotmart-principal",
                                              plataforma="hotmart", total_esperado=100))
        self.ex = captura.LocalExecutor(
            self.cursos, motor_python=PY, motor_dir=DIR, spawn=self.sp,
            lock_dir=str(tmp_path / "locks"), pid_vivo=self.sp.mundo.vivo,
            motor_log_dir=str(tmp_path / "logs"), pendencias_fn=self._pendencias,
            estado_cursos_path=self.path)

    def terminar(self, url, codigo, saida):
        call = self.chamadas(url)[-1]
        path = self.ex._stderr_path(self.ex.conta_de(url))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(saida)
        call["proc"].encerrar(codigo)


def _reiniciar_conta_dupla(mac, pend, agora):
    mac2 = _MacContaDupla(mac.tmp, pend, sp=mac.sp)
    mac2.estado = athena_local._carregar_estado_cursos(mac2.path, agora=agora)
    sy = athena_local._carregar_estado_youtube(mac2.path, agora=agora)
    if sy:
        mac2.estado[athena_local._CHAVE_YOUTUBE] = sy
    return mac2


def test_a_conta_da_prova_orfa_morta_fica_travada_ate_a_autopsia_e_o_err_nao_se_perde(tmp_path):
    pend = {CIRO: {"youtube": 15}, CIRO2: {"base": 5}}
    mac = _MacContaDupla(tmp_path, pend)
    t = _prova_do_ciro(mac)
    mac2 = _reiniciar_conta_dupla(mac, pend, t + 60)
    # a órfã morre depois da autópsia; na MESMA passada vem o outro curso da conta dela
    _ciclo_com_morte_no_meio(mac2, t + 120, [CIRO, CIRO2], CIRO, 4,
                             _ABORTO_YOUTUBE + _linha("bloqueado"))
    assert mac2.chamadas(CIRO2) == [], (
        "a conta da prova órfã foi reaberta antes da autópsia — o disparo trunca o .err dela")
    mac2.ciclo(t + 300, [CIRO, CIRO2])
    disco = mac2.disco()
    assert disco.get("degrau") == 2 and "prova_curso" not in disco, disco
    mac2.ciclo(t + 480, [CIRO, CIRO2])                     # resolvida: a conta volta a trabalhar
    assert len(mac2.chamadas(CIRO2)) == 1


def test_prova_orfa_cujo_lock_sumiu_ainda_e_autopsiada_pelo_err_do_pid_dela(tmp_path):
    mac = _Mac3(tmp_path, _PEND)
    t = _prova_do_ciro(mac)
    mac2 = _reiniciar(mac, _PEND, t + 60)
    mac2.terminar(CIRO, 4, _ABORTO_YOUTUBE + _linha("bloqueado"))   # a órfã morre...
    os.remove(mac2.ex._lock_path("hotmart-principal"))              # ... e o lock some
    err = mac2.ex._stderr_path("hotmart-principal")
    os.utime(err, (t + 90, t + 90))                                 # o .err é deste disparo
    mac2.ciclo(t + 120, [CIRO])
    mac2.ciclo(t + 300, [CIRO])
    disco = mac2.disco()
    assert "prova_curso" not in disco and disco.get("degrau") == 2, (
        f"a prova órfã sem lock ficou sem resultado: {disco}")


# ==========================================================================
# Exigência de segurança da instalação (motor M4 + D3r2 na mesma janela): SEM a linha
# `YOUTUBE_PROVA` NUNCA sai prova imediata — exit 0 sem linha, SIGTERM, a órfã vista pelo lock
# (exit_code=None) e a órfã que nunca é autopsiada (o teto). A suspensão continua com
# `ate` = agora + janela(degrau atual); no ciclo seguinte ninguém leva prova; vencido o novo `ate`,
# sai UMA.
# ==========================================================================
@pytest.mark.parametrize("modo", ["exit0-sem-linha", "sigterm", "orfa-pelo-lock-sem-linha",
                                  "orfa-sem-autopsia-no-teto"])
def test_sem_a_linha_da_prova_nunca_sai_prova_imediata(tmp_path, modo):
    pend = {CIRO: {"youtube": 15}, OUTRO: {"youtube": 8}, MK: {"base": 4}}
    todos = [CIRO, OUTRO, MK]
    mac = _Mac3(tmp_path, pend)
    t = _prova_do_ciro(mac)
    t1 = t + 180
    if modo == "exit0-sem-linha":
        mac.terminar(CIRO, 0, _STATS_OK)
    elif modo == "sigterm":
        mac.terminar(CIRO, -15, "")
    elif modo == "orfa-pelo-lock-sem-linha":
        mac = _reiniciar(mac, pend, t + 60)                # o daemon reinicia; a órfã sai 0 sem linha
        mac.terminar(CIRO, 0, _STATS_OK)
    else:
        mac = _reiniciar(mac, pend, t + 60)                # a órfã fica pendurada, sem resultado
        t1 = t + 24 * H + 600
    n0 = len(mac.sp.calls)
    mac.ciclo(t1, todos)                                   # ciclo 1: a prova sem linha (ou o teto)
    disco = mac.disco()
    assert disco.get("ate") == t1 + 3 * H and disco.get("degrau") == 1, disco
    assert "prova_curso" not in disco, disco
    mac.ciclo(t1 + 180, todos)                             # ciclo 2: relógio 3 min à frente
    soltas = [c["cmd"] for c in mac.sp.calls[n0:] if "--youtube" in c["cmd"] or ENV_PROVA in c["env"]]
    assert soltas == [], f"a prova sem linha soltou outra na hora ({modo}): {soltas}"
    mac.ciclo(t1 + 3 * H + 60, todos)                      # ciclo 3: o novo `ate` venceu
    provas = [c["cmd"] for c in mac.sp.calls[n0:] if ENV_PROVA in c["env"]]
    assert len(provas) == 1, provas


def test_o_curso_da_prova_nao_e_redisparado_nem_quando_o_lock_da_orfa_some_no_meio_do_ciclo(
        tmp_path):
    # B1(c) sozinho: com o lock da órfã mantido, a criação exclusiva já barra o redisparo. Sem lock
    # (sumiu no meio do ciclo, depois da autópsia), só a regra do curso da prova impede o `base`
    # do portador de subir sem token — e o lock NOVO dele esconderia a órfã da autópsia por PID.
    pend = {CIRO: {"youtube": 15}, MK: {"base": 4}}
    mac = _Mac3(tmp_path, pend)
    t = _abrir_suspensao(mac) + 3 * H + 60
    mac.ciclo(t, [MK])
    assert ENV_PROVA in mac.chamadas(MK)[-1]["env"]
    mac2 = _reiniciar(mac, pend, t + 60)
    lock = mac2.ex._lock_path("memberkit-yt")
    err = mac2.ex._stderr_path("memberkit-yt")
    terminar = mac2.terminar

    def terminar_e_o_lock_some(url, codigo, saida):
        terminar(url, codigo, saida)
        os.remove(lock)
        os.utime(err, (t + 150, t + 150))

    mac2.terminar = terminar_e_o_lock_some
    _ciclo_com_morte_no_meio(mac2, t + 120, [MK], MK, 0,
                             "Memberkit: total=4 ok=0 audio=0 falhou=4\n" + _linha("bloqueado"))
    assert len(mac2.chamadas(MK)) == 1, "o curso da prova órfã foi redisparado sem token"
    mac2.ciclo(t + 300, [MK])
    disco = mac2.disco()
    assert disco.get("degrau") == 2 and "prova_curso" not in disco, disco


def test_err_mais_velho_que_a_prova_nao_vira_resultado_dela(tmp_path):
    # um .err de ANTES da prova (o tee falhou no disparo dela) pode ter a linha `ok` de outra prova
    mac = _Mac3(tmp_path, _PEND)
    t = _prova_do_ciro(mac)
    mac2 = _reiniciar(mac, _PEND, t + 60)
    mac2.terminar(CIRO, 0, _STATS_OK + _linha("ok"))
    os.remove(mac2.ex._lock_path("hotmart-principal"))
    err = mac2.ex._stderr_path("hotmart-principal")
    os.utime(err, (t - 3600, t - 3600))
    mac2.ciclo(t + 120, [CIRO])
    disco = mac2.disco()
    assert disco.get("ate") == t + 120 + 3 * H and disco.get("degrau") == 1, (
        f"um .err velho decidiu a prova: {disco}")
    assert "prova_curso" not in disco, disco
