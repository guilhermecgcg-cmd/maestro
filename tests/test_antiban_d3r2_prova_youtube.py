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

Dublês: os do D3 (`_Mac3`: o LocalExecutor REAL com spawn de mentira e o ciclo REAL do daemon).
"""
import pytest

from tests.test_antiban_d3_prova_youtube import (  # noqa: F401
    CIRO, ENV_PROVA, H, OUTRO, _linha, _Mac3, _prova_do_ciro, _STATS_OK)
from tests.test_antiban_r2_suspensao_youtube import (  # noqa: F401 (fixture autouse)
    _ABORTO_YOUTUBE, _passe_youtube_ligado)
from tests.test_antiban_relancamento_exit4 import _ABORT_EXIT4, _sem_tracker_vivo  # noqa: F401

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
