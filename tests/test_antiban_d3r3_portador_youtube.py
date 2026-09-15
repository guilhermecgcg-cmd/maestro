"""D3r3 — os PORTADORES da prova do YouTube (revisão do motor M4 sobre o 99287e9).

ITEM 1: a revisão do M4 deu NO-GO pelo lado do motor — na prova, a Entrega Digital e a Hubla
continuam pedindo ao YouTube depois do bloqueio. Elas saem da lista de portadores até o motor
provar "prova ED/Hubla com 3 aulas bot → exatamente 1 pedido, linha `bloqueado`, exit 4 youtube".
Continuam portadores: `--youtube` do Hotmart, memberkit, greenn, nutror e alpaclass.
  - com a janela vencida, um curso ED ou Hubla dispara SEM token e não reserva a prova;
  - durante a suspensão, o passe normal delas sai sem token (o M4 barra o YouTube no motor) e a
    saída limpa sem avanço é o cooldown de sempre — nunca a espera neutra de portador.

Dublês: os do D3 (`_Mac3`: o LocalExecutor REAL com spawn de mentira e o ciclo REAL do daemon),
com cursos extras.
"""
import pytest

from maestro.adaptadores import captura
from tests.test_antiban_d3_prova_youtube import (  # noqa: F401
    CIRO, ENV_PROVA, H, MK, _abrir_suspensao, _Mac3)
from tests.test_antiban_r2_suspensao_youtube import _passe_youtube_ligado  # noqa: F401
from tests.test_antiban_relancamento_exit4 import _sem_tracker_vivo  # noqa: F401
from tests.test_executor_local import DIR, PY

ED = "https://luanacarolina.entregadigital.app.br/"
HB = "https://app.hub.la/m/curso-hubla"
_EXTRAS = {ED: ("ed-principal", "entregadigital"), HB: ("hubla-principal", "hubla")}


class _MacCom(_Mac3):
    """O Mac do D3 com os cursos Entrega Digital e Hubla."""

    def __init__(self, tmp_path, pend):
        super().__init__(tmp_path, pend)
        for url, (conta, plat) in _EXTRAS.items():
            self.cursos.append(captura.CursoLocal(
                url=url, conta=conta, plataforma=plat, total_esperado=100,
                session_path=str(tmp_path / f"{conta}-session.json")))
        self.ex = captura.LocalExecutor(
            self.cursos, motor_python=PY, motor_dir=DIR, spawn=self.sp,
            lock_dir=str(tmp_path / "locks"), pid_vivo=self.sp.mundo.vivo,
            motor_log_dir=str(tmp_path / "logs"), pendencias_fn=self._pendencias,
            estado_cursos_path=self.path)

    def terminar(self, url, codigo, saida):
        import os
        call = self.chamadas(url)[-1]
        path = self.ex._stderr_path(self.ex.conta_de(url))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(saida)
        call["proc"].encerrar(codigo)


@pytest.mark.parametrize("url", [ED, HB], ids=["entregadigital", "hubla"])
def test_entregadigital_e_hubla_nao_levam_a_prova_com_a_janela_vencida(tmp_path, url):
    mac = _MacCom(tmp_path, {CIRO: {"youtube": 15}, url: {"base": 3}, MK: {"base": 3}})
    t = _abrir_suspensao(mac) + 3 * H + 60                 # a janela venceu
    mac.ciclo(t, [url])
    call = mac.chamadas(url)[-1]
    assert ENV_PROVA not in call["env"], f"{url} levou o token da prova"
    assert "prova_curso" not in mac.disco(), f"{url} reservou a prova: {mac.disco()}"
    mac.ciclo(t + 180, [MK])                               # a prova segue esperando um portador
    assert ENV_PROVA in mac.chamadas(MK)[-1]["env"] and mac.disco().get("prova_curso") == MK


@pytest.mark.parametrize("url", [ED, HB], ids=["entregadigital", "hubla"])
def test_entregadigital_e_hubla_na_suspensao_saem_para_o_cooldown_e_nao_esperam_a_janela(tmp_path,
                                                                                         url):
    mac = _MacCom(tmp_path, {CIRO: {"youtube": 15}, url: {"base": 3}})
    t_aut = _abrir_suspensao(mac)                          # janela de 3 h aberta
    mac.ciclo(t_aut + 120, [url])
    assert len(mac.chamadas(url)) == 1 and ENV_PROVA not in mac.chamadas(url)[-1]["env"]
    mac.terminar(url, 0, "Stats: total=3 ok=0 audio=0 falhou=3\n")     # o M4 barrou o YouTube
    mac.ciclo(t_aut + 240, [url])
    st = mac.estado[url]
    assert not st.get("espera_youtube"), f"{url} virou portador em espera pela janela: {st}"
    assert st.get("cooldown_ate") == t_aut + 240 + 6 * H, st
    mac.ciclo(t_aut + 3 * H + 60, [url])                   # vencida a janela: não sai (cooldown)...
    assert len(mac.chamadas(url)) == 1
    mac.ciclo(t_aut + 240 + 6 * H + 60, [url])             # ... e volta quando o cooldown vence
    assert len(mac.chamadas(url)) == 2 and ENV_PROVA not in mac.chamadas(url)[-1]["env"]
