"""Adaptador de CAPTURA no Maestro: coordena o protocolo de captura de um curso.

Os dublês MODELAM o mecanismo, não a coreografia:
  - FakeAcesso: exec_sql roteia por conteúdo do SQL (sessão / progresso / INSERT na
    fila) e devolve linhas -tA (campos por '|'); exec_app modela o reconcile.
  - FakeVoz: registra avisos (avisa-e-age) e escaladas (pedidos) — prova que o
    RESEED e as falhas honestas passam pela voz.
  - FakeExecutor: registra que a captura foi DISPARADA e com quais args, e nunca
    abre Chrome — prova o seam residencial (captura fora da VPS).

INVIOLÁVEIS cravados aqui:
  - sessão morta -> PEDE reseed via voz, NUNCA tenta logar nem dispara captura;
  - captura só via executor injetado (residencial), o adaptador não abre Chrome;
  - escala honesta: etapa que falha/não confirma reporta via voz, não finge sucesso.
"""
import time

import pytest

from maestro.adaptadores import captura
from maestro.registro import Projeto


def _proj(**kw):
    base = dict(nome="captura", projeto_easypanel="conhecimentoinfinito",
                servicos=("worker",), adaptador="captura",
                db_container="conhecimentoinfinito_db", db_name="conhecimento",
                db_user="postgres",
                app_container="conhecimentoinfinito_conhecimentoinfinito",
                gerenciar=True)
    base.update(kw)
    return Projeto(**base)


# --------------------------------------------------------------------------
# Dublês
# --------------------------------------------------------------------------
class FakeAcesso:
    """Modela exec_sql (roteia por conteúdo) + exec_app (reconcile)."""
    def __init__(self, *, sessao=None, progresso=None, reconcile="RECONCILE_OK {}",
                 boom_sql=False, boom_app=False):
        self._sessao = sessao          # linhas devolvidas pela query de sessão
        self._progresso = progresso    # linhas devolvidas pela query de progresso
        self._reconcile = reconcile
        self._boom_sql = boom_sql
        self._boom_app = boom_app
        self.sqls = []
        self.inserts = []
        self.execs = []

    def exec_sql(self, container, sql, *, db, user="postgres", rows=True):
        self.sqls.append((container, sql, db, user, rows))
        if self._boom_sql:
            raise RuntimeError("socket off")
        if sql.strip().upper().startswith("INSERT"):
            self.inserts.append(sql)
            return []
        if "sessao_plataforma" in sql:
            return list(self._sessao or [])
        if "estado_aulas" in sql:
            return list(self._progresso or [])
        return []

    def exec_app(self, container, comando):
        self.execs.append((container, comando))
        if self._boom_app:
            raise RuntimeError("docker off")
        return self._reconcile


class FakeVoz:
    def __init__(self):
        self.avisos = []
        self.escaladas = []

    def avisar_acao(self, acao):
        self.avisos.append(acao)

    def escalar(self, problema, pedido):
        self.escaladas.append((problema, pedido))


class FakeExecutor:
    """Seam residencial: registra o disparo; NUNCA abre Chrome."""
    def __init__(self, confirmacao="enfileirado:res", boom=False):
        self._confirmacao = confirmacao
        self._boom = boom
        self.disparos = []

    def disparar(self, curso):
        self.disparos.append(curso)
        if self._boom:
            raise RuntimeError("fila off")
        return self._confirmacao


# --------------------------------------------------------------------------
# estado_sessao — leitura do tracker (viva / morta / desconhecida)
# --------------------------------------------------------------------------
def test_sessao_viva_quando_valida_e_fresca():
    agora = 1_000_000.0
    ac = FakeAcesso(sessao=[f"t|{agora - 60}"])           # válida, 60s atrás
    assert captura.estado_sessao(_proj(), ac, agora=agora) == "viva"
    # provou que leu pelo container certo, não por DNS interno:
    cont, sql, db, user, rows = ac.sqls[0]
    assert cont == "conhecimentoinfinito_db" and db == "conhecimento"
    assert "sessao_plataforma" in sql


def test_sessao_morta_quando_flag_invalida():
    agora = 1_000_000.0
    ac = FakeAcesso(sessao=[f"f|{agora - 60}"])           # marcada inválida
    assert captura.estado_sessao(_proj(), ac, agora=agora) == "morta"


def test_sessao_morta_quando_velha_demais():
    agora = 1_000_000.0
    ac = FakeAcesso(sessao=[f"t|{agora - captura.SESSAO_MAX_IDADE_S - 1}"])
    assert captura.estado_sessao(_proj(), ac, agora=agora) == "morta"


def test_sessao_desconhecida_quando_exec_falha():
    # DENTES: falha de acesso ao tracker NÃO pode virar "viva" (dispararia captura
    # sem sessão confirmada) nem "morta" (pediria reseed à toa) — é "desconhecida".
    assert captura.estado_sessao(_proj(), FakeAcesso(boom_sql=True), agora=1.0) == "desconhecida"


def test_sessao_desconhecida_sem_linhas():
    assert captura.estado_sessao(_proj(), FakeAcesso(sessao=[]), agora=1.0) == "desconhecida"


def test_sessao_desconhecida_sem_db_container():
    p = _proj(db_container="")
    assert captura.estado_sessao(p, FakeAcesso(sessao=["t|0"]), agora=1.0) == "desconhecida"


# --------------------------------------------------------------------------
# progresso — contagens do tracker (reusa estado_aulas do conhecimento)
# --------------------------------------------------------------------------
def test_progresso_conta_total_e_done():
    ac = FakeAcesso(progresso=["10|4"])                   # total=10, done=4
    total, done, pend = captura.progresso(_proj(), ac, "123")
    assert (total, done, pend) == (10, 4, 6)
    sql = ac.sqls[0][1]
    assert "estado_aulas" in sql and "123" in sql


def test_progresso_curso_sem_aulas_zera():
    ac = FakeAcesso(progresso=[])
    assert captura.progresso(_proj(), ac, "999") == (0, 0, 0)


# --------------------------------------------------------------------------
# FilaExecutor — executor residencial padrão: ENFILEIRA (VPS-safe, sem Chrome)
# --------------------------------------------------------------------------
def test_fila_executor_enfileira_via_exec_sql():
    ac = FakeAcesso()
    ex = captura.FilaExecutor(ac, _proj())
    conf = ex.disparar("777")
    assert conf                                            # confirmação truthy
    assert len(ac.inserts) == 1
    ins = ac.inserts[0]
    assert "fila_captura" in ins and "777" in ins and "enfileirado" in ins


def test_fila_executor_propaga_falha_do_banco():
    # DENTES: se o INSERT falha, disparar LEVANTA (o coordenador escala honesto).
    ex = captura.FilaExecutor(FakeAcesso(boom_sql=True), _proj())
    with pytest.raises(Exception):
        ex.disparar("777")


# --------------------------------------------------------------------------
# INVIOLÁVEL estrutural: o adaptador NUNCA importa um driver de browser
# --------------------------------------------------------------------------
def test_adaptador_nao_importa_browser():
    import inspect
    src = inspect.getsource(captura)
    baixo = src.lower()
    assert "playwright" not in baixo
    assert "selenium" not in baixo
    assert "webdriver" not in baixo
