"""Revisão adversarial independente do D3 — testes mínimos que FALHAVAM contra 5f99956.

Copiados de `rev-d3-testes/test_revisao_d3_achados.py` como regressão, um por commit do D3r2. Os
do guarded restart (A3, A4) moram em `test_ops_guarded_restart.py`, junto dos dublês do script.
"""
from tests.test_antiban_d3_prova_youtube import (  # noqa: F401
    CIRO, ENV_PROVA, H, MK, OUTRO, _abrir_suspensao, _linha, _Mac3, _prova_do_ciro)
from tests.test_antiban_r2_suspensao_youtube import (  # noqa: F401 (fixture autouse)
    _ABORTO_YOUTUBE, _passe_youtube_ligado)
from tests.test_antiban_relancamento_exit4 import _sem_tracker_vivo  # noqa: F401


# A1 — prova que BATEU no YouTube (exit 4 classe youtube) mas diz nao_tocou: a evidência do abort
# é jogada fora, o degrau não sobe e a PRÓXIMA prova sai 3 min depois, noutra conta.
def test_exit4_classe_youtube_com_linha_nao_tocou_nao_solta_outra_prova_na_hora(tmp_path):
    mac = _Mac3(tmp_path, {CIRO: {"youtube": 15}, OUTRO: {"youtube": 8}})
    t = _prova_do_ciro(mac)
    mac.terminar(CIRO, 4, _ABORTO_YOUTUBE + _linha("nao_tocou"))
    mac.ciclo(t + 180, [CIRO, OUTRO])
    disco = mac.disco()
    assert mac.youtube(OUTRO) == [], (
        f"exit 4 classe youtube + nao_tocou: 2ª prova (--youtube de outra conta) 3 min depois; "
        f"disco={disco}")
    assert disco.get("degrau") == 2, disco


# A2 — gravação do estado falhando com a janela VENCIDA: o `base` do portador sai sem token, sai 0
# sem avanço, vira espera neutra e é SOLTO no mesmo ciclo (a janela venceu) — redisparo a cada
# ciclo contra a plataforma paga, sem cooldown.
def test_gravacao_falhando_com_janela_vencida_nao_redispara_o_portador_todo_ciclo(tmp_path):
    mac = _Mac3(tmp_path, {CIRO: {"youtube": 15}, MK: {"base": 4}})
    t_aut = _abrir_suspensao(mac)
    mac.falhar_gravacao = True
    t = t_aut + 3 * H + 60
    mac.ciclo(t, [MK])
    assert len(mac.chamadas(MK)) == 1 and ENV_PROVA not in mac.chamadas(MK)[-1]["env"]
    for k in range(1, 6):
        mac.terminar(MK, 0, "Memberkit: total=4 ok=0 audio=0 falhou=4\n")
        mac.ciclo(t + k * 180, [MK])
    assert len(mac.chamadas(MK)) <= 2, (
        f"{len(mac.chamadas(MK))} disparos do Memberkit em 15 min, todos sem avanço e sem token")
