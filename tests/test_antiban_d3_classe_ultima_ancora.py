"""D3, ITEM 5 (achado 2 da revisão independente) — na saída inteira vence a ÚLTIMA âncora, e a
parede da plataforma tem âncora própria.

O ACHADO: desde 535fb28 a classe do exit 4 é decidida sobre os últimos 64 KB da saída do motor.
A regra "vence a ocorrência mais recente" só comparava as âncoras LOCAL e YOUTUBE; a classe
`plataforma` não tinha âncora de prosa — era só o padrão. Numa saída em que uma frase local antiga
aparece centenas de linhas acima e a parede é impressa por último, a local era a ÚNICA âncora
encontrada e vencia: a parede virava "provedor local", não contava na série e o curso não benchava.

AGORA: as âncoras da parede ("sistematicamente errado do lado d", "Continuar martelando é o
caminho do banimento" — as frases do `CircuitBreakerError` tipo plataforma do motor) competem por
POSIÇÃO com as locais e as do YouTube; vence a última. A linha `ABORT_DISJUNTOR tipo=…` continua
vencendo tudo.
"""
import pytest

from tests.test_antiban_exit4_classe_e_expiracao import (_ABORT_LOCAL, _ABORT_YOUTUBE_SUSPENSO,
                                                        _abortar)
from tests.test_antiban_r2_cauda_inteira import _RUIDO_HTTPX
from tests.test_antiban_relancamento_exit4 import (_ABORT_EXIT4, _Daemon,
                                                   _sem_tracker_vivo)  # noqa: F401 (autouse)

_LONGE = _RUIDO_HTTPX * 7                                 # ~420 linhas entre as duas frases


def test_abort_local_antigo_seguido_da_parede_impressa_por_ultimo_e_parede(tmp_path):
    cauda = _ABORT_LOCAL + _LONGE + _ABORT_EXIT4
    assert len(cauda) < 64 * 1024                         # as duas frases cabem na saída bruta
    d = _Daemon(tmp_path)
    _abortar(d, cauda, 3)
    assert d.st.get("benched_exit4") is True, "a frase local antiga venceu a parede impressa por último"


@pytest.mark.parametrize("cauda,bencha", [
    (_ABORT_EXIT4 + _LONGE + _ABORT_LOCAL, False),
    (_ABORT_YOUTUBE_SUSPENSO + _LONGE + _ABORT_EXIT4, True),
    (_ABORT_EXIT4 + _LONGE + _ABORT_YOUTUBE_SUSPENSO, False),
    (_ABORT_LOCAL + _LONGE + _ABORT_EXIT4 + "ABORT_DISJUNTOR tipo=local\n", False),
    (_ABORT_EXIT4 + _LONGE + _ABORT_LOCAL + "ABORT_DISJUNTOR tipo=plataforma\n", True),
], ids=["parede-antiga-local-por-ultimo", "youtube-antigo-parede-por-ultimo",
        "parede-antiga-youtube-por-ultimo", "maquina-local-vence-parede-por-ultimo",
        "maquina-plataforma-vence-local-por-ultimo"])
def test_vence_a_ultima_ancora_e_a_linha_de_maquina_vence_tudo(tmp_path, cauda, bencha):
    d = _Daemon(tmp_path)
    _abortar(d, cauda, 3)
    assert bool(d.st.get("benched_exit4")) is bencha
