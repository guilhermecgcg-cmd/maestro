"""RODADA 2, ITEM 2 — a captura ÓRFÃ que abortou no disjuntor é tratada como exit 4.

O ACHADO (anterior ao ramo): a morte vista só pelo lock (PID morto de uma encarnação anterior do
loop — o Popen se perdeu com ela) chega com `exit_code=None` (`vigia.autopsia`, detectado_por
"pid"), e o bloco do exit 4 só age com `exit_code == 4`. A órfã que abortou na parede era
relançada no ciclo da própria autópsia: sem espera, sem série, sem bench.

AGORA: sem exit code, a saída inteira decide. Vira exit 4 quando traz a linha
`ABORT_DISJUNTOR tipo=…`, o cabeçalho dos CLIs antes do `SystemExit(4)` ("RUN INTERROMPIDO POR
EXCESSO DE FALHAS", "PARADO POR BLOQUEIO DO YOUTUBE", "YOUTUBE SUSPENSO NO PASSE", "VIERAM COMO
INDISPONÍVEIS") ou a linha "RUN ABORTADO com N/M aulas concluídas:" COM mensagem de disjuntor.

DIVERGÊNCIA TÉCNICA COM A PROPOSTA ("RUN ABORTADO" basta): no motor 2cf9d04 o mesmo
`log.error("RUN ABORTADO com %d/%d aulas concluídas: %s")` sai no `except (SessionLostError,
CircuitBreakerError, LockPerdido)` do `run_course` — sessão morta (exit 2/3), sonda inconclusiva
(exit 5, subclasse de SessionLostError) e lock perdido também o imprimem. Tomar a frase sozinha
por exit 4 poria esses no ramo errado; o último teste prende isso.

Dublê: lock real em disco com PID que não pode existir (acima do pid_max do macOS) e o .err da
conta num temporário — o mesmo caminho do vigia vivo (`stderr_path_de`).
"""
import json
import os

import pytest

from maestro import athena_local, causa, disjuntor, vigia
from tests.test_antiban_exit4_classe_e_expiracao import _ABORT_LOCAL
from tests.test_antiban_r2_cauda_inteira import _RUIDO_HTTPX
from tests.test_antiban_relancamento_exit4 import (CONTA, T0, VIRAL, _Alertas,
                                                   _sem_tracker_vivo)  # noqa: F401 (autouse)
from tests.test_athena_integracao import FakeExecutorObitos, FakeVoz, _curso, _prog

PID_INEXISTENTE = 4_194_311              # > pid_max do macOS (99 998): nunca existe

_CLI_PAREDE = (
    "2026-09-15 11:07:24,101 ERROR motor.orchestrator: RUN ABORTADO com 7/300 aulas concluídas: "
    "5 erros nas últimas 5 aulas. Algo está sistematicamente errado do lado da Cademí — pode ser "
    "o anti-bot, a sessão, a rede, ou um detector nosso ruim. Continuar martelando é o caminho do "
    "banimento. Parando com ok=7 audio=0 falhou=5 de 300.\n"
    "\n⛔ RUN INTERROMPIDO POR EXCESSO DE FALHAS: 5 erros nas últimas 5 aulas. Algo está "
    "sistematicamente errado do lado da Cademí. Parando com ok=7 audio=0 falhou=5 de 300.\n"
    "\nO QUE FAZER — nesta ordem:\n1. OLHE OS ERROS no tracker antes de qualquer coisa.\n")
_CLI_YOUTUBE_SUSPENSO = (
    "\n⛔ YOUTUBE SUSPENSO NO PASSE — BLOQUEIO DO YOUTUBE: o YouTube ficou fora do alcance deste "
    "IP neste passe. NÃO é a plataforma do curso e NÃO é a conta: é o caminho até o YouTube. "
    "Passe Memberkit: ok=4 audio=0 falhou=2 de 6.\n")
_SO_A_LINHA_DE_MAQUINA = "encerrando o passe\nABORT_DISJUNTOR tipo=local\n"
_RUN_ABORTADO_POR_SONDA = (
    "2026-09-15 11:07:24,101 ERROR motor.orchestrator: RUN ABORTADO com 0/15 aulas concluídas: "
    "sonda de /v1/navigation inconclusiva (timeout) — sem veredito, parei por precaução\n")


class _ExecutorComErr(FakeExecutorObitos):
    """O executor de óbitos com o `.err` por conta (o seam que o vigia usa na morte só-de-lock)."""

    def __init__(self, conta_de, err_dir):
        super().__init__(conta_de)
        self._err_dir = str(err_dir)

    def _stderr_path(self, conta):
        return os.path.join(self._err_dir, f"{conta}.err")


def _ciclo_com_orfa(tmp_path, cauda, *, agora=T0):
    """Uma captura de encarnação ANTERIOR (lock com PID morto + .err dela) e UM ciclo do daemon."""
    locks = tmp_path / "locks"
    locks.mkdir(exist_ok=True)
    (locks / "orfa.lock").write_text(json.dumps(
        {"pid": PID_INEXISTENTE, "course_url": VIRAL, "conta": CONTA, "ts": agora}))
    ex = _ExecutorComErr({VIRAL: CONTA}, tmp_path / "err")
    os.makedirs(ex._err_dir, exist_ok=True)
    with open(ex._stderr_path(CONTA), "w", encoding="utf-8") as f:
        f.write(cauda)                                     # gravado DEPOIS do lock (mesmo disparo)
    estado, alertas = {}, _Alertas()
    athena_local.ciclo_local(
        [_curso(VIRAL, CONTA, "cademi", 300)], ex, _prog({VIRAL: (100, 300)}), FakeVoz(), {},
        estado, agora=agora, disjuntor=disjuntor, vigia=vigia, causa=causa, alertas=alertas,
        lock_dir=str(locks), autopsia_dir=str(tmp_path / "aut"), boot_ts=1.0)
    return ex, estado.get(VIRAL, {})


@pytest.mark.parametrize("cauda,conta_na_serie", [
    (_CLI_PAREDE, True),
    (_CLI_YOUTUBE_SUSPENSO, False),
    (_SO_A_LINHA_DE_MAQUINA, False),
], ids=["cli-parede", "cli-youtube-suspenso", "linha-de-maquina"])
def test_orfa_que_abortou_no_disjuntor_nao_e_relancada_no_ciclo_da_autopsia(tmp_path, cauda,
                                                                           conta_na_serie):
    ex, st = _ciclo_com_orfa(tmp_path, cauda)
    assert ex.disparos == [], "a órfã que abortou no disjuntor foi relançada na hora"
    assert st.get("exit4_ate", 0) > T0, st
    assert bool(st.get("exit4_seguidas")) is conta_na_serie, st


def test_orfa_local_com_ruido_e_classificada_pela_saida_inteira(tmp_path):
    ex, st = _ciclo_com_orfa(tmp_path, _ABORT_LOCAL + _RUIDO_HTTPX)
    assert ex.disparos == [] and st.get("exit4_ate", 0) > T0, st
    assert not st.get("exit4_seguidas"), st


def test_RUN_ABORTADO_sem_mensagem_de_disjuntor_nao_vira_exit4(tmp_path):
    # sonda inconclusiva / sessão / lock perdido também imprimem "RUN ABORTADO com N/M"
    _ex, st = _ciclo_com_orfa(tmp_path, _RUN_ABORTADO_POR_SONDA)
    assert "exit4_ate" not in st and "exit4_seguidas" not in st, st
