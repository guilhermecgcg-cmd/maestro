"""Revisão independente do D3r2 (99287e9, GO sem bloqueador) — achado 1 [média-baixa], no D3r3.

O ACHADO: a autópsia da prova órfã SEM LOCK (`_fonte_da_prova_sem_lock`, B1(d)) só descartava o
.err mais velho que a prova, e o bloqueio de redisparo (B1(c)) travava o CURSO da prova, não a
CONTA. Com o lock apagado à mão, outro curso da mesma conta subia, sobrescrevia o .err, e a autópsia
pelo PID lia a saída dele: o `bloqueado` real da prova se perdia (degrau 1 em vez de 2) e a causa da
morte alheia caía no curso da prova.

AGORA:
  - enquanto existir `prova_curso`, nenhum curso da MESMA CONTA da prova é disparado;
  - o executor grava, a cada disparo, o DONO do .err da conta (`<slug>.err.dono`: pid, curso, ts). A
    autópsia pelo PID só aceita o .err se o dono é o PID e o curso da prova (e o .err não é mais
    velho que ela). Sem essa prova, a prova é resolvida como SEM LINHA e nenhuma causa vai ao curso
    da prova — nem autópsia em disco, nem disjuntor, nem exit 4.

Dublês: os do D3r2 (a conta dupla do Ciro, com o spawn que trunca o .err como o `_spawn_popen`).
"""
import glob
import json
import os

from tests.test_antiban_d3_prova_youtube import (  # noqa: F401
    CIRO, H, _linha, _prova_do_ciro)
from tests.test_antiban_d3r2_prova_youtube import CIRO2, _MacContaDupla, _reiniciar_conta_dupla
from tests.test_antiban_r2_suspensao_youtube import (  # noqa: F401 (fixture autouse)
    _ABORTO_YOUTUBE, _passe_youtube_ligado)
from tests.test_antiban_relancamento_exit4 import _ABORT_EXIT4, _sem_tracker_vivo  # noqa: F401
from tests.test_revisao_d3_reinicio_na_prova import _ciclo_com_morte_no_meio

_PEND = {CIRO: {"youtube": 15}, CIRO2: {"base": 5}}
_CONTA = "hotmart-principal"


def test_lock_apagado_e_outro_curso_da_conta_nao_sobe_nem_troca_o_resultado_da_prova(tmp_path):
    mac = _MacContaDupla(tmp_path, _PEND)
    t = _prova_do_ciro(mac)
    mac2 = _reiniciar_conta_dupla(mac, _PEND, t + 60)
    lock = mac2.ex._lock_path(_CONTA)
    terminar = mac2.terminar

    def terminar_e_apagar_o_lock(url, codigo, saida):
        terminar(url, codigo, saida)
        os.remove(lock)                                    # o lock da órfã apagado à mão

    mac2.terminar = terminar_e_apagar_o_lock
    _ciclo_com_morte_no_meio(mac2, t + 120, [CIRO, CIRO2], CIRO, 4,
                             _ABORTO_YOUTUBE + _linha("bloqueado"))
    mac2.terminar = terminar
    subiu_com_a_prova_aberta = []
    if mac2.chamadas(CIRO2) and mac2.disco().get("prova_curso"):
        subiu_com_a_prova_aberta.append(t + 120)
    for k in range(1, 4):
        ultimo = mac2.chamadas(CIRO2)[-1:]
        if ultimo and ultimo[0]["proc"].poll() is None:    # o run do outro curso bate na parede
            terminar(CIRO2, 4, _ABORT_EXIT4)
        antes = len(mac2.chamadas(CIRO2))
        mac2.ciclo(t + 120 + k * 180, [CIRO, CIRO2])
        if len(mac2.chamadas(CIRO2)) > antes and mac2.disco().get("prova_curso"):
            subiu_com_a_prova_aberta.append(t + 120 + k * 180)
    disco = mac2.disco()
    assert "prova_curso" not in disco and disco.get("degrau") == 2, (
        f"o bloqueado da prova se perdeu: {disco}")
    assert not mac2.estado[CIRO].get("exit4_seguidas"), (
        f"a parede do outro curso caiu no curso da prova: {mac2.estado[CIRO]}")
    assert subiu_com_a_prova_aberta == [], (
        f"outro curso da conta subiu com a prova sem resultado: {subiu_com_a_prova_aberta}")


def test_err_de_outro_run_da_conta_vira_prova_sem_linha_e_nenhuma_causa_no_curso_da_prova(tmp_path):
    mac = _MacContaDupla(tmp_path, _PEND)
    t = _prova_do_ciro(mac)
    mac2 = _reiniciar_conta_dupla(mac, _PEND, t + 60)
    pid_da_prova = mac2.disco()["prova_pid"]
    mac2.terminar(CIRO, 0, "")                             # a órfã morre, o lock some, e um run de
    os.remove(mac2.ex._lock_path(_CONTA))                  # OUTRO curso da conta (um daemon anterior)
    err = mac2.ex._stderr_path(_CONTA)                     # deixou a saída dele no .err da conta
    with open(err, "w", encoding="utf-8") as f:
        f.write(_ABORT_EXIT4 + _linha("ok"))
    with open(err + ".dono", "w") as f:
        json.dump({"pid": pid_da_prova + 1, "course_url": CIRO2, "ts": t + 90}, f)
    os.utime(err, (t + 100, t + 100))                      # e não é mais velho que a prova
    antes = dict(mac2.estado.get(CIRO, {}))
    autopsias = len(glob.glob(str(tmp_path / "aut" / "*.json")))
    mac2.ciclo(t + 120, [CIRO])
    disco = mac2.disco()
    assert disco.get("ate") == t + 120 + 3 * H and disco.get("degrau") == 1, (
        f"a saída de outro run decidiu a prova: {disco}")
    assert "prova_curso" not in disco, disco
    st = mac2.estado[CIRO]
    assert not st.get("exit4_seguidas") and st.get("disj_falhas") == antes.get("disj_falhas"), st
    assert len(glob.glob(str(tmp_path / "aut" / "*.json"))) == autopsias, (
        "a morte sem saída provada virou autópsia do curso da prova")
