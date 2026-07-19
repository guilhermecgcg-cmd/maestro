"""PONTE AUTO-INGEST no Maestro: Acesso.exec_app (docker exec de rotina) + a
rotina periodica reconciliar (adaptador conhecimento) + o roteamento no loop.

Os dubles MODELAM o mecanismo: exec_app roda via run_cmd (o mesmo seam do
exec_sql); reconciliar decide a janela e confirma sucesso pela sentinela
RECONCILE_OK que o `python -m conhecimento.reconcile` imprime.
"""
import time

import pytest

from maestro.acesso import Acesso
from maestro.adaptadores import conhecimento
from maestro.adaptadores.conhecimento import INTERVALO_RECONCILE_S
from maestro.registro import Projeto


def _proj(**kw):
    base = dict(nome="conhecimento", projeto_easypanel="conhecimentoinfinito",
                servicos=("conhecimentoinfinito",), adaptador="conhecimento",
                app_container="conhecimentoinfinito_conhecimentoinfinito",
                gerenciar=True)
    base.update(kw)
    return Projeto(**base)


# ---- Acesso.exec_app -------------------------------------------------------
def test_exec_app_monta_docker_exec_e_captura_stderr():
    cap = {}
    def fake_run(cmd):
        cap["cmd"] = cmd
        return "RECONCILE_OK {'ingeridas': 2}\n"
    a = Acesso(run_cmd=fake_run)
    saida = a.exec_app("cont_app", "python -m conhecimento.reconcile")
    assert "RECONCILE_OK" in saida
    c = cap["cmd"]
    assert "docker exec -i" in c
    assert "docker ps -qf name=cont_app" in c        # descobre o container pelo socket
    assert "python -m conhecimento.reconcile" in c
    assert "2>&1" in c                               # DENTES: sem isto, traceback (stderr) se perde


def test_exec_app_levanta_quando_container_nao_existe():
    a = Acesso(run_cmd=lambda cmd: "__EXEC_FAIL__no_container\n")
    with pytest.raises(RuntimeError):
        a.exec_app("nao_existe", "python -m conhecimento.reconcile")


# ---- reconciliar (rotina periodica) ----------------------------------------
class FakeAcesso:
    def __init__(self, saida="RECONCILE_OK {'ingeridas': 3, 'puladas': 1}", boom=False):
        self._saida = saida
        self._boom = boom
        self.execs = []
    def exec_app(self, container, comando):
        self.execs.append((container, comando))
        if self._boom:
            raise RuntimeError("docker off")
        return self._saida


def test_reconciliar_dispara_quando_janela_venceu():
    ac = FakeAcesso()
    agora = 10_000.0
    ultimo = agora - INTERVALO_RECONCILE_S - 1        # janela vencida
    acao, novo = conhecimento.reconciliar(_proj(), ac, agora=agora, ultimo=ultimo)
    assert acao.executada and not acao.escalar
    assert novo == agora                              # marca o disparo
    assert ac.execs == [("conhecimentoinfinito_conhecimentoinfinito",
                         "python -m conhecimento.reconcile")]
    assert "reconcile" in acao.descricao


def test_reconciliar_pula_dentro_da_janela_sem_tocar_container():
    ac = FakeAcesso()
    agora = 10_000.0
    ultimo = agora - 60                               # janela AINDA nao venceu
    acao, novo = conhecimento.reconciliar(_proj(), ac, agora=agora, ultimo=ultimo)
    assert acao is None and novo == ultimo
    assert ac.execs == []                             # NAO chamou docker exec (barato)


def test_reconciliar_sem_app_container_nao_faz_nada():
    ac = FakeAcesso()
    acao, novo = conhecimento.reconciliar(_proj(app_container=""), ac,
                                          agora=10_000.0, ultimo=0.0)
    assert acao is None and ac.execs == []


def test_reconciliar_falha_de_exec_escala_honesto():
    # DENTES: se o docker exec falha, NAO pode dizer "reconciliei" -- tem que escalar.
    ac = FakeAcesso(boom=True)
    acao, novo = conhecimento.reconciliar(_proj(), ac, agora=10_000.0, ultimo=0.0)
    assert acao.escalar and not acao.executada
    assert novo == 10_000.0                            # nao re-tenta em looping no mesmo ciclo
    assert "FALHOU" in acao.pedido


def test_reconciliar_sem_sentinela_escala_nao_finge_sucesso():
    # DENTES: saida sem RECONCILE_OK (ex.: erro que saiu 0 por acidente) NAO e sucesso.
    ac = FakeAcesso(saida="Traceback (most recent call last): ...")
    acao, novo = conhecimento.reconciliar(_proj(), ac, agora=10_000.0, ultimo=0.0)
    assert acao.escalar and not acao.executada
    assert "sem confirma" in acao.pedido.lower() or "RECONCILE_OK" in acao.pedido


# ---- roteamento no loop ----------------------------------------------------
from maestro.loop import ciclo
from maestro.acesso import Servico


class _Voz:
    def __init__(self): self.avisos = []; self.escaladas = []
    def avisar_acao(self, a): self.avisos.append(a)
    def escalar(self, p, pedido): self.escaladas.append(pedido)


class _AcessoLoop:
    def __init__(self, saida="RECONCILE_OK {'ingeridas': 1}"):
        self._saida = saida
        self.execs = []
    def servicos(self): return {"conhecimentoinfinito": Servico("conhecimentoinfinito", True, False)}
    def saude_http(self, alvos): return {}
    def recursos(self): return {"disco_pct": 10, "ram_pct": 10}
    def logs(self, nome, n=50): return ""
    def restart(self, nome): pass
    def exec_sql(self, container, sql, *, db, user="postgres", rows=True): return []
    def exec_app(self, container, comando):
        self.execs.append((container, comando)); return self._saida


def test_loop_dispara_reconcile_no_primeiro_ciclo_e_respeita_janela():
    a = _AcessoLoop(); v = _Voz()
    proj = _proj(servicos=("conhecimentoinfinito",))
    estado = {}
    ciclo(a, v, [proj], llm=lambda p: "{}", estado=estado)
    assert len(a.execs) == 1 and a.execs[0][1] == "python -m conhecimento.reconcile"
    assert any("reconcile" in x.descricao for x in v.avisos)
    # 2o ciclo logo em seguida: janela nao venceu -> NAO dispara de novo.
    ciclo(a, v, [proj], llm=lambda p: "{}", estado=estado)
    assert len(a.execs) == 1                           # continua 1: estado segurou


def test_loop_projeto_monitorado_nao_dispara_reconcile():
    a = _AcessoLoop(); v = _Voz()
    proj = _proj(servicos=("conhecimentoinfinito",), gerenciar=False)
    ciclo(a, v, [proj], llm=lambda p: "{}", estado={})
    assert a.execs == []                               # monitorado nao age
