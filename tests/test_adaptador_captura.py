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


# --------------------------------------------------------------------------
# coordenar — orquestrador do protocolo, por curso, com estado entre ciclos
# --------------------------------------------------------------------------
AGORA = 1_000_000.0


def _sessao_viva():
    return [f"t|{AGORA - 60}"]


# ---- FASE 1: checar sessão (INVIOLÁVEL anti-login) --------------------------
def test_sessao_morta_pede_reseed_e_nao_dispara_captura():
    # O inviolável mais importante: sessão morta NÃO tenta logar, NÃO dispara a
    # captura — PEDE reseed via voz.
    ac = FakeAcesso(sessao=[f"f|{AGORA - 60}"])
    voz = FakeVoz()
    ex = FakeExecutor()
    estado = {}
    acao = captura.coordenar(_proj(), ac, voz, executor=ex, curso="55",
                             estado=estado, agora=AGORA)
    assert acao.escalar and not acao.executada
    assert ex.disparos == []                       # NÃO disparou captura
    assert ac.inserts == []                        # nem enfileirou
    assert len(voz.escaladas) == 1
    pedido = voz.escaladas[0][1].lower()
    assert "reseed" in pedido or "re-semear" in pedido
    assert "login" not in pedido or "não" in pedido  # jamais promete logar
    assert estado.get("fase") != captura.FASE_CAPTURANDO  # não avançou


def test_sessao_desconhecida_escala_honesto_e_nao_dispara():
    # exec falhou -> não sabemos da sessão -> NÃO dispara às cegas; escala.
    ac = FakeAcesso(boom_sql=True)
    voz = FakeVoz()
    ex = FakeExecutor()
    acao = captura.coordenar(_proj(), ac, voz, executor=ex, curso="55",
                             estado={}, agora=AGORA)
    assert acao.escalar and not acao.executada
    assert ex.disparos == []
    assert len(voz.escaladas) == 1


# ---- FASE 2: disparar captura via EXECUTOR residencial ---------------------
def test_sessao_viva_dispara_via_executor_e_avisa():
    ac = FakeAcesso(sessao=_sessao_viva())
    voz = FakeVoz()
    ex = FakeExecutor(confirmacao="enfileirado:55")
    estado = {}
    acao = captura.coordenar(_proj(), ac, voz, executor=ex, curso="55",
                             estado=estado, agora=AGORA)
    assert acao.executada and not acao.escalar
    assert ex.disparos == ["55"]                   # captura foi delegada ao executor
    assert estado["fase"] == captura.FASE_CAPTURANDO
    assert len(voz.avisos) == 1
    assert "55" in acao.descricao


def test_disparo_que_levanta_escala_honesto_sem_avancar():
    # DENTES: executor que falha NÃO pode virar "capturando" nem avisar sucesso.
    ac = FakeAcesso(sessao=_sessao_viva())
    voz = FakeVoz()
    ex = FakeExecutor(boom=True)
    estado = {}
    acao = captura.coordenar(_proj(), ac, voz, executor=ex, curso="55",
                             estado=estado, agora=AGORA)
    assert acao.escalar and not acao.executada
    assert estado.get("fase") != captura.FASE_CAPTURANDO
    assert voz.avisos == [] and len(voz.escaladas) == 1


def test_disparo_sem_confirmacao_escala_honesto():
    # DENTES: executor que devolve falsy = sem confirmação -> não assume sucesso.
    ac = FakeAcesso(sessao=_sessao_viva())
    voz = FakeVoz()
    ex = FakeExecutor(confirmacao="")
    estado = {}
    acao = captura.coordenar(_proj(), ac, voz, executor=ex, curso="55",
                             estado=estado, agora=AGORA)
    assert acao.escalar and not acao.executada
    assert estado.get("fase") != captura.FASE_CAPTURANDO


# ---- FASE 3: monitorar progresso -------------------------------------------
def test_capturando_incompleto_fica_quieto_sem_ingest():
    ac = FakeAcesso(progresso=["10|4"])            # 4 de 10 -> ainda rodando
    voz = FakeVoz()
    ex = FakeExecutor()
    estado = {"fase": captura.FASE_CAPTURANDO}
    acao = captura.coordenar(_proj(), ac, voz, executor=ex, curso="55",
                             estado=estado, agora=AGORA)
    assert acao is None                            # nada a reportar (não spamma)
    assert ac.execs == []                          # NÃO disparou auto-ingest
    assert estado["fase"] == captura.FASE_CAPTURANDO


def test_progresso_inacessivel_escala_honesto():
    ac = FakeAcesso(boom_sql=True)
    voz = FakeVoz()
    estado = {"fase": captura.FASE_CAPTURANDO}
    acao = captura.coordenar(_proj(), ac, voz, executor=FakeExecutor(), curso="55",
                             estado=estado, agora=AGORA)
    assert acao.escalar and not acao.executada
    assert len(voz.escaladas) == 1


# ---- FASE 4: concluído -> auto-ingest reusando conhecimento.reconciliar -----
def test_curso_completo_dispara_autoingest_via_reconciliar():
    ac = FakeAcesso(progresso=["10|10"], reconcile="RECONCILE_OK {'ingeridas': 10}")
    voz = FakeVoz()
    estado = {"fase": captura.FASE_CAPTURANDO}
    acao = captura.coordenar(_proj(), ac, voz, executor=FakeExecutor(), curso="55",
                             estado=estado, agora=AGORA)
    assert acao.executada and not acao.escalar
    # provou o REUSO: passou pelo exec_app do reconcile no container de app
    assert ac.execs == [("conhecimentoinfinito_conhecimentoinfinito",
                         "python -m conhecimento.reconcile")]
    assert estado["fase"] == captura.FASE_CONCLUIDO
    assert any("reconcile" in a.descricao for a in voz.avisos)


def test_autoingest_que_falha_escala_e_nao_marca_concluido():
    # DENTES: reconcile que falha NÃO pode marcar o curso como concluído.
    ac = FakeAcesso(progresso=["10|10"], boom_app=True)
    voz = FakeVoz()
    estado = {"fase": captura.FASE_CAPTURANDO}
    acao = captura.coordenar(_proj(), ac, voz, executor=FakeExecutor(), curso="55",
                             estado=estado, agora=AGORA)
    assert acao.escalar and not acao.executada
    assert estado["fase"] != captura.FASE_CONCLUIDO


def test_autoingest_sem_sentinela_escala_nao_finge_sucesso():
    ac = FakeAcesso(progresso=["10|10"], reconcile="Traceback: boom")
    voz = FakeVoz()
    estado = {"fase": captura.FASE_CAPTURANDO}
    acao = captura.coordenar(_proj(), ac, voz, executor=FakeExecutor(), curso="55",
                             estado=estado, agora=AGORA)
    assert acao.escalar and not acao.executada
    assert estado["fase"] != captura.FASE_CONCLUIDO


def test_autoingest_sem_app_container_escala():
    # curso completo mas projeto sem alvo de ingest -> honesto, não some silencioso.
    ac = FakeAcesso(progresso=["10|10"])
    voz = FakeVoz()
    estado = {"fase": captura.FASE_CAPTURANDO}
    acao = captura.coordenar(_proj(app_container=""), ac, voz,
                             executor=FakeExecutor(), curso="55",
                             estado=estado, agora=AGORA)
    assert acao.escalar and not acao.executada
    assert ac.execs == []


def test_concluido_nao_redispara_nada():
    ac = FakeAcesso(sessao=_sessao_viva(), progresso=["10|10"])
    voz = FakeVoz()
    ex = FakeExecutor()
    estado = {"fase": captura.FASE_CONCLUIDO}
    acao = captura.coordenar(_proj(), ac, voz, executor=ex, curso="55",
                             estado=estado, agora=AGORA)
    assert acao is None
    assert ex.disparos == [] and ac.execs == []    # idempotente e quieto


# ---- Protocolo completo ao longo de ciclos (estado persiste) ---------------
def test_protocolo_completo_novo_ate_concluido():
    voz = FakeVoz()
    ex = FakeExecutor(confirmacao="enfileirado:55")
    estado = {}

    # ciclo 1: sessão viva -> dispara
    ac1 = FakeAcesso(sessao=_sessao_viva())
    a1 = captura.coordenar(_proj(), ac1, voz, executor=ex, curso="55",
                           estado=estado, agora=AGORA)
    assert a1.executada and estado["fase"] == captura.FASE_CAPTURANDO

    # ciclo 2: ainda capturando -> quieto, não redispara
    ac2 = FakeAcesso(progresso=["10|3"])
    a2 = captura.coordenar(_proj(), ac2, voz, executor=ex, curso="55",
                           estado=estado, agora=AGORA)
    assert a2 is None and ex.disparos == ["55"]     # disparou UMA vez só

    # ciclo 3: completou -> auto-ingest -> concluído
    ac3 = FakeAcesso(progresso=["10|10"], reconcile="RECONCILE_OK {}")
    a3 = captura.coordenar(_proj(), ac3, voz, executor=ex, curso="55",
                           estado=estado, agora=AGORA)
    assert a3.executada and estado["fase"] == captura.FASE_CONCLUIDO
    assert ac3.execs and ac3.execs[0][1] == "python -m conhecimento.reconcile"


# ---- SEAM da esteira (classificador/sintetizador) — declarado, não implementado
def test_seam_esteira_existe_e_e_noop():
    # O ponto de extensão para o classificador fino e o Sintetizador (cursos how_to)
    # existe e é um NO-OP honesto: não faz nada e não finge que fez.
    assert hasattr(captura, "_hooks_esteira")
    assert captura._hooks_esteira(_proj(), "55", {}) == []
