"""Revisão adversarial independente do D3 — testes mínimos que FALHAVAM contra 5f99956.

Copiados de `rev-d3-testes/test_revisao_d3_achados.py` como regressão, um por commit do D3r2. Os
do guarded restart (A3, A4) moram em `test_ops_guarded_restart.py`, junto dos dublês do script.
"""
from tests.test_antiban_d3_prova_youtube import (  # noqa: F401
    CIRO, OUTRO, _linha, _Mac3, _prova_do_ciro)
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
