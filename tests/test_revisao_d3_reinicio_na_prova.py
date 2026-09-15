"""Revisão D3 — o daemon REINICIA enquanto a prova do YouTube roda (pedido do coordenador).

Copiado de `rev-d3-testes/test_revisao_d3_reinicio_na_prova.py` como regressão do D3r2, B1.

Encarnação 1: `_Mac3` dispara a prova (token no env, reserva no disco). "Queda": nasce a encarnação
2 com um LocalExecutor NOVO (sem o Popen da prova, que vira ÓRFÃ), o MESMO lock_dir/log/estado, e o
`estado` recarregado do DISCO (`_carregar_estado_cursos` + `_carregar_estado_youtube`, como o
`rodar`). O mundo de processos é o mesmo: a órfã segue viva até o teste a encerrar.
"""
import pytest

from maestro import athena_local, causa, disjuntor, vigia
from maestro.adaptadores import captura
from tests.test_antiban_d3_prova_youtube import (  # noqa: F401
    CIRO, ENV_PROVA, H, MK, OUTRO, _abrir_suspensao, _linha, _Mac3, _prova_do_ciro,
    _STATS_OK)
from tests.test_antiban_r2_suspensao_youtube import (  # noqa: F401 (fixture autouse)
    _ABORTO_YOUTUBE, _passe_youtube_ligado)
from tests.test_antiban_relancamento_exit4 import _sem_tracker_vivo  # noqa: F401
from tests.test_executor_local import DIR, PY


def _reiniciar(mac, pend, agora):
    """A encarnação 2: executor novo, estado relido do disco, o MESMO mundo de processos."""
    mac2 = _Mac3(mac.tmp, pend)
    mac2.sp = mac.sp
    mac2.ex = captura.LocalExecutor(
        mac2.cursos, motor_python=PY, motor_dir=DIR, spawn=mac.sp,
        lock_dir=str(mac.tmp / "locks"), pid_vivo=mac.sp.mundo.vivo,
        motor_log_dir=str(mac.tmp / "logs"), pendencias_fn=mac2._pendencias,
        estado_cursos_path=mac2.path)
    mac2.estado = athena_local._carregar_estado_cursos(mac2.path, agora=agora)
    sy = athena_local._carregar_estado_youtube(mac2.path, agora=agora)
    if sy:
        mac2.estado[athena_local._CHAVE_YOUTUBE] = sy
    return mac2


def _ciclo_com_morte_no_meio(mac2, agora, cursos, url, codigo, saida):
    """Um ciclo em que a órfã `url` morre DEPOIS da autópsia e ANTES da passada dela (a leitura do
    Notion do curso é o gancho: vem logo antes do `curso_ativo`)."""
    feito = []

    def progresso(c):
        if c == url and not feito:
            feito.append(1)
            mac2.terminar(url, codigo, saida)
        return (10, 100)

    lista = [c for c in mac2.cursos if c.url in cursos]
    athena_local.ciclo_local(
        lista, mac2.ex, progresso, mac2.voz, mac2.voo, mac2.estado, agora=agora,
        disjuntor=disjuntor, vigia=vigia, causa=causa, alertas=mac2.alertas,
        lock_dir=str(mac2.tmp / "locks"), autopsia_dir=str(mac2.tmp / "aut"), boot_ts=1.0,
        persistir_estado=lambda: athena_local._gravar_estado_cursos(mac2.path, mac2.estado))
    athena_local._gravar_estado_cursos(mac2.path, mac2.estado)


_PEND = {CIRO: {"youtube": 15}, OUTRO: {"youtube": 8}, MK: {"base": 4}}


# Q1 + Q2 (caminho feliz): a reserva volta do disco, ninguém mais leva token nem --youtube, e a
# órfã que morre ENTRE ciclos é autopsiada pelo lock com a linha da prova.
@pytest.mark.parametrize("codigo,saida,esperado", [
    (0, _STATS_OK + _linha("ok"), "encerra"),
    (4, _ABORTO_YOUTUBE + _linha("bloqueado"), "degrau2"),
])
def test_reinicio_com_prova_rodando_resultado_da_orfa_nao_se_perde(tmp_path, codigo, saida,
                                                                     esperado):
    mac = _Mac3(tmp_path, _PEND)
    t = _prova_do_ciro(mac)
    token = mac.chamadas(CIRO)[-1]["env"][ENV_PROVA]
    mac2 = _reiniciar(mac, _PEND, t + 60)
    assert mac2.estado[athena_local._CHAVE_YOUTUBE].get("prova_curso") == CIRO
    n_antes = len(mac2.sp.calls)
    mac2.ciclo(t + 120, [CIRO, OUTRO, MK])
    novos = mac2.sp.calls[n_antes:]
    assert [c for c in novos if ENV_PROVA in c["env"]] == [], "a encarnação 2 soltou outra prova"
    assert mac2.youtube(OUTRO) == [] and mac2.disco().get("prova_curso") == CIRO
    assert str(float(mac2.disco()["prova_desde"])) == token
    mac2.terminar(CIRO, codigo, saida)                     # a órfã morre com o daemon dormindo
    mac2.ciclo(t + 300, [CIRO])
    disco = mac2.disco()
    assert "prova_curso" not in disco, disco
    if esperado == "encerra":
        assert "ate" not in disco, disco
    else:
        assert disco.get("degrau") == 2, disco


# Q2 (buraco): a órfã morre DURANTE o ciclo, depois da autópsia. `curso_ativo` (passada) — ou o
# pulso cheio do fim de ciclo — APAGA o lock de PID morto (`_ler_lock(limpar=True)`) e a morte
# nunca é autopsiada: o `bloqueado` da prova se perde; a reserva fica até o teto de 24 h.
def test_orfa_da_prova_que_morre_no_meio_do_ciclo_ainda_tem_o_resultado_lido(tmp_path):
    mac = _Mac3(tmp_path, _PEND)
    t = _prova_do_ciro(mac)
    mac2 = _reiniciar(mac, _PEND, t + 60)
    _ciclo_com_morte_no_meio(mac2, t + 120, [CIRO], CIRO, 4,
                             _ABORTO_YOUTUBE + _linha("bloqueado"))
    for k in range(1, 4):
        mac2.ciclo(t + 120 + k * 180, [CIRO])
    disco = mac2.disco()
    assert "prova_curso" not in disco and disco.get("degrau") == 2, (
        f"o resultado 'bloqueado' da órfã se perdeu: {disco}")


# Q2/Q3 (atribuição errada): prova num portador `base` (Memberkit). A órfã morre no meio do ciclo,
# o lock some na passada e a MESMA passada redispara o curso SEM token; a morte desse run (que não
# pôde tocar o YouTube) é lida como o resultado da prova.
# ADAPTADO no D3r2 (B1(c), regra do coordenador): o curso da prova NÃO é redisparado enquanto ela
# não tem resultado — o original aceitava também o redisparo sem token com a atribuição certa. E a
# órfã que morre no meio do ciclo é lida pela autópsia do ciclo SEGUINTE (a mesma espera de UM
# ciclo de toda morte ainda não autopsiada); o original exigia o resultado no mesmo ciclo quando
# não havia redisparo.
def test_run_seguinte_sem_token_nao_vira_resultado_da_prova_orfa(tmp_path):
    pend = {CIRO: {"youtube": 15}, MK: {"base": 4}}
    mac = _Mac3(tmp_path, pend)
    t_aut = _abrir_suspensao(mac)
    t = t_aut + 3 * H + 60
    mac.ciclo(t, [MK])
    assert ENV_PROVA in mac.chamadas(MK)[-1]["env"]
    mac2 = _reiniciar(mac, pend, t + 60)
    _ciclo_com_morte_no_meio(mac2, t + 120, [MK], MK, 0,
                             "Memberkit: total=4 ok=0 audio=0 falhou=4\n" + _linha("bloqueado"))
    assert len(mac2.chamadas(MK)) == 1, "o portador da prova órfã foi redisparado sem token"
    mac2.ciclo(t + 300, [MK])                              # a autópsia do ciclo seguinte lê a órfã
    disco = mac2.disco()
    assert disco.get("degrau") == 2 and "prova_curso" not in disco, (
        f"a prova 'bloqueado' virou {disco}")
