"""Adaptador de CAPTURA no Maestro: coordena o protocolo de captura de um curso.

Os dublês MODELAM O SCHEMA REAL, não a coreografia (I2 — dublês COM DENTES):
  - FakeAcesso: exec_sql roteia por conteúdo e IMITA o Postgres real —
      * `sessao_plataforma` NÃO EXISTE -> levanta como o psql levantaria (C1);
      * INSERT sem coluna course_url -> viola NOT NULL (C2);
      * ON CONFLICT que não seja em (course_url) -> não casa índice real (C2);
      * estado_aulas.course_id é TEXT -> comparar com int cru levanta (C3);
      * o FILTER de progresso é computado a partir da lista de terminais do PRÓPRIO
        SQL, então errar a lista de terminais muda a contagem (I1).
  - FakeVoz: registra avisos (avisa-e-age) e escaladas (pedidos).
  - FakeExecutor: registra que a captura foi DISPARADA (por course_url), nunca abre
    Chrome — prova o seam residencial (captura fora da VPS).

Cada teste de C1/C2/C3/I1 FALHA se o bug for reintroduzido (teeth verificados).
"""
import re

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
# Dublês COM DENTES — modelam o schema real de produção
# --------------------------------------------------------------------------
class FakeAcesso:
    """exec_sql roteia por conteúdo e IMITA as restrições reais do Postgres.

    Parâmetros:
      sessao_morta : há job em STATUS_SESSAO_MORTA para a URL? (sinal do worker)
      resolve      : course_id (str) que a fila resolveu, ou "" / None = ainda não
      aulas        : lista de STATUS de aulas — o progresso computa total + não-terminais
      reconcile    : saída do exec_app (reconcile)
      boom_sql     : todo exec_sql levanta (banco inacessível)
      boom_aulas   : só a query de estado_aulas levanta (progresso inacessível)
      boom_app     : exec_app levanta (reconcile inacessível)
    """
    def __init__(self, *, sessao_morta=False, resolve=None, aulas=None,
                 reconcile="RECONCILE_OK {}", boom_sql=False, boom_aulas=False,
                 boom_app=False):
        self._sessao_morta = sessao_morta
        self._resolve = resolve
        self._aulas = aulas
        self._reconcile = reconcile
        self._boom_sql = boom_sql
        self._boom_aulas = boom_aulas
        self._boom_app = boom_app
        self.sqls = []
        self.inserts = []
        self.execs = []

    def exec_sql(self, container, sql, *, db, user="postgres", rows=True):
        self.sqls.append((container, sql, db, user, rows))
        if self._boom_sql:
            raise RuntimeError("socket off")
        up = sql.upper()
        # C1: `sessao_plataforma` NÃO EXISTE no schema real -> psql levantaria.
        if "SESSAO_PLATAFORMA" in up:
            raise RuntimeError('relation "sessao_plataforma" does not exist')
        if up.lstrip().startswith("INSERT"):
            return self._insert(sql)
        if "ESTADO_AULAS" in up:
            return self._progresso_rows(sql)
        if "FILA_CAPTURA" in up and captura.STATUS_SESSAO_MORTA.upper() in up:
            return ["1"] if self._sessao_morta else []
        if "FILA_CAPTURA" in up:                       # resolve course_id
            return [self._resolve] if self._resolve else []
        return []

    def _insert(self, sql):
        up = sql.upper()
        if "FILA_CAPTURA" not in up:
            raise RuntimeError("relation for INSERT não modelada")
        self.inserts.append(sql)
        m = re.search(r"fila_captura\s*\(([^)]*)\)", sql, re.I)
        cols = [c.strip().lower() for c in m.group(1).split(",")] if m else []
        # C2: course_url é NOT NULL no schema real.
        if "course_url" not in cols:
            raise RuntimeError('null value in column "course_url" violates not-null constraint')
        # C2: o índice único REAL é PARCIAL sobre (course_url); ON CONFLICT em
        # qualquer outro alvo (ex.: o antigo course_id) não casa -> psql levanta.
        if "ON CONFLICT" in up:
            mc = re.search(r"ON CONFLICT\s*\(([^)]*)\)", sql, re.I)
            alvo = mc.group(1).strip().lower() if mc else ""
            if alvo != "course_url":
                raise RuntimeError("no unique or exclusion constraint matching the "
                                   "ON CONFLICT specification")
        return []

    def _progresso_rows(self, sql):
        if self._boom_aulas:
            raise RuntimeError("progresso off")
        # C3: estado_aulas.course_id é TEXT -> o literal DEVE vir quotado. Um int cru
        # (WHERE course_id = 123) levanta como o psql: text = integer não existe.
        m = re.search(r"course_id\s*=\s*(\S+)", sql, re.I)
        if m and m.group(1)[:1] != "'":
            raise RuntimeError("operator does not exist: text = integer")
        if self._aulas is None:
            return []
        # I1: modela o FILTER real — pendentes = aulas com status NÃO-terminal, lendo
        # a lista de terminais do PRÓPRIO SQL. Se o código errar a lista, isto muda.
        mt = re.search(r"NOT IN\s*\(([^)]*)\)", sql, re.I)
        terminais = {t.strip().strip("'").lower() for t in mt.group(1).split(",")} if mt else set()
        total = len(self._aulas)
        pend = sum(1 for s in self._aulas if s.lower() not in terminais)
        return [f"{total}|{pend}"]

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
    """Seam residencial: registra o disparo (por course_url); NUNCA abre Chrome."""
    def __init__(self, confirmacao="enfileirado:res", boom=False):
        self._confirmacao = confirmacao
        self._boom = boom
        self.disparos = []

    def disparar(self, curso_url):
        self.disparos.append(curso_url)
        if self._boom:
            raise RuntimeError("fila off")
        return self._confirmacao


AGORA = 1_000_000.0
URL = "https://plataforma/curso/42"


# ==========================================================================
# C1 — estado_sessao: sinal de sessão vem do STATUS do job na fila (real)
# ==========================================================================
def test_estado_sessao_morta_le_sinal_da_fila():
    ac = FakeAcesso(sessao_morta=True)
    assert captura.estado_sessao(_proj(), ac, URL) == "morta"
    sql = ac.sqls[0][1]
    # DENTES C1: lê de fila_captura pelo status de contrato, NÃO de sessao_plataforma
    # (se voltasse a consultar sessao_plataforma, o dublê levantaria -> desconhecida).
    assert "fila_captura" in sql and captura.STATUS_SESSAO_MORTA in sql
    assert "sessao_plataforma" not in sql


def test_estado_sessao_desconhecida_sem_sinal_de_morte():
    # Sem sinal de morte -> 'desconhecida' (NÃO 'viva': liveness não é derivável na VPS).
    assert captura.estado_sessao(_proj(), FakeAcesso(sessao_morta=False), URL) == "desconhecida"


def test_estado_sessao_desconhecida_quando_exec_falha():
    # Falha de acesso não vira 'morta' (reseed à toa) nem 'viva' (dispara às cegas).
    assert captura.estado_sessao(_proj(), FakeAcesso(boom_sql=True), URL) == "desconhecida"


def test_estado_sessao_desconhecida_sem_db_container():
    assert captura.estado_sessao(_proj(db_container=""),
                                 FakeAcesso(sessao_morta=True), URL) == "desconhecida"


# ==========================================================================
# C2 — FilaExecutor.disparar: enfileira por course_url (contrato real da fila)
# ==========================================================================
def test_fila_executor_enfileira_por_course_url():
    ac = FakeAcesso()
    conf = captura.FilaExecutor(ac, _proj()).disparar(URL)
    assert conf                                            # confirmação truthy
    assert len(ac.inserts) == 1
    ins = ac.inserts[0]
    # DENTES C2: precisa citar course_url (NOT NULL) e a própria URL; sem course_id.
    assert "fila_captura" in ins and "course_url" in ins and URL in ins
    assert "'enfileirado'" in ins


def test_fila_executor_on_conflict_casa_indice_real_course_url():
    # DENTES C2: ON CONFLICT tem de casar o índice único PARCIAL real (course_url).
    # O dublê levanta se o alvo for outro (ex.: o antigo course_id).
    ac = FakeAcesso()
    captura.FilaExecutor(ac, _proj()).disparar(URL)       # não levanta => casou
    assert "ON CONFLICT (course_url)" in ac.inserts[0]


def test_fila_executor_escapa_aspas_na_url():
    ac = FakeAcesso()
    captura.FilaExecutor(ac, _proj()).disparar("a'b")
    assert "'a''b'" in ac.inserts[0]                      # anti-injeção por escaping


def test_fila_executor_propaga_falha_do_banco():
    ex = captura.FilaExecutor(FakeAcesso(boom_sql=True), _proj())
    with pytest.raises(Exception):
        ex.disparar(URL)


# ==========================================================================
# C3 — progresso: course_id é TEXT (quotado + escapado, nunca int cru)
# ==========================================================================
def test_progresso_conta_e_quota_course_id_text():
    ac = FakeAcesso(aulas=["no_notion"] * 4 + ["pendente"] * 6)  # total 10, pend 6
    total, done, pend = captura.progresso(_proj(), ac, "abc'123")
    assert (total, done, pend) == (10, 4, 6)
    sql = ac.sqls[0][1]
    # DENTES C3: course_id quotado E aspas escapadas (int() quebraria em 'abc'123').
    assert "estado_aulas" in sql
    assert "'abc''123'" in sql


def test_progresso_curso_sem_aulas_zera():
    assert captura.progresso(_proj(), FakeAcesso(aulas=[]), "999") == (0, 0, 0)


# ==========================================================================
# I1 — "done" = nenhuma aula em estado NÃO-terminal (não trava pra sempre)
# ==========================================================================
def test_curso_so_com_terminais_benignos_e_falhas_conclui():
    # I1: aulas em sem_legenda/sem_video/falhou/... são TERMINAIS -> curso CONCLUI.
    # DENTES: se a lista de terminais omitisse 'falhou' (ou outro), o FILTER do dublê
    # contaria pendentes>0 e o curso ficaria capturando pra sempre -> este teste falha.
    ac = FakeAcesso(resolve="999",
                    aulas=["no_notion", "anexos_baixados", "sem_legenda", "sem_video",
                           "sem_embed", "sem_audio", "audio_erro", "falhou",
                           "transcrevendo_embed"],
                    reconcile="RECONCILE_OK {}")
    voz = FakeVoz()
    estado = {"fase": captura.FASE_CAPTURANDO}
    acao = captura.coordenar(_proj(), ac, voz, executor=FakeExecutor(),
                             curso_url=URL, estado=estado, agora=AGORA)
    assert acao.executada and estado["fase"] == captura.FASE_CONCLUIDO


def test_curso_com_aula_nao_terminal_fica_quieto():
    ac = FakeAcesso(resolve="999", aulas=["no_notion", "pendente"])  # 1 não-terminal
    estado = {"fase": captura.FASE_CAPTURANDO}
    acao = captura.coordenar(_proj(), ac, FakeVoz(), executor=FakeExecutor(),
                             curso_url=URL, estado=estado, agora=AGORA)
    assert acao is None and estado["fase"] == captura.FASE_CAPTURANDO


# ==========================================================================
# C4 — identidade: dispara por course_url, monitora por course_id resolvido
# ==========================================================================
def test_monitora_por_course_id_resolvido_nao_por_url():
    ac = FakeAcesso(resolve="777", aulas=["no_notion"] * 3)  # completo
    estado = {"fase": captura.FASE_CAPTURANDO}
    captura.coordenar(_proj(), ac, FakeVoz(), executor=FakeExecutor(),
                      curso_url=URL, estado=estado, agora=AGORA)
    sql_aulas = next(s[1] for s in ac.sqls if "estado_aulas" in s[1])
    # DENTES C4: monitorou pelo course_id RESOLVIDO da fila, não pela URL.
    assert "'777'" in sql_aulas
    assert URL not in sql_aulas


def test_capturando_sem_course_id_aguarda_reivindicacao():
    # C4: worker ainda não reivindicou -> course_id nulo. NÃO é erro nem falha: fica
    # quieto e espera. NÃO monitora estado_aulas às cegas, NÃO escala.
    ac = FakeAcesso(resolve="")
    voz = FakeVoz()
    estado = {"fase": captura.FASE_CAPTURANDO}
    acao = captura.coordenar(_proj(), ac, voz, executor=FakeExecutor(),
                             curso_url=URL, estado=estado, agora=AGORA)
    assert acao is None
    assert voz.escaladas == []
    assert not any("estado_aulas" in s[1] for s in ac.sqls)
    assert estado["fase"] == captura.FASE_CAPTURANDO


# ==========================================================================
# FASE 1 — sessão morta pede reseed / anti-login (inviolável)
# ==========================================================================
def test_sessao_morta_pede_reseed_e_nao_dispara_captura():
    ac = FakeAcesso(sessao_morta=True)
    voz = FakeVoz()
    ex = FakeExecutor()
    estado = {}
    acao = captura.coordenar(_proj(), ac, voz, executor=ex, curso_url=URL,
                             estado=estado, agora=AGORA)
    assert acao.escalar and not acao.executada
    assert ex.disparos == []                       # NÃO disparou captura
    assert ac.inserts == []                        # nem enfileirou
    assert len(voz.escaladas) == 1
    pedido = voz.escaladas[0][1].lower()
    assert "reseed" in pedido or "re-semear" in pedido
    assert "login" not in pedido or "não" in pedido  # jamais promete logar
    assert estado.get("fase") != captura.FASE_CAPTURANDO


# ==========================================================================
# FASE 2 — sessão desconhecida NÃO bloqueia; dispara via executor residencial
# ==========================================================================
def test_sessao_desconhecida_nao_bloqueia_dispara_e_avisa():
    # C1 (contrato novo): sem sinal de morte, o Maestro SEGUE enfileirando — o worker
    # residencial é o detector real. 'desconhecida' NÃO escala nem bloqueia.
    ac = FakeAcesso(sessao_morta=False)
    voz = FakeVoz()
    ex = FakeExecutor(confirmacao="enfileirado:42")
    estado = {}
    acao = captura.coordenar(_proj(), ac, voz, executor=ex, curso_url=URL,
                             estado=estado, agora=AGORA)
    assert acao.executada and not acao.escalar
    assert ex.disparos == [URL]                    # captura delegada ao executor
    assert estado["fase"] == captura.FASE_CAPTURANDO
    assert voz.escaladas == [] and len(voz.avisos) == 1
    assert URL in acao.descricao


def test_disparo_que_levanta_escala_honesto_sem_avancar():
    ac = FakeAcesso(sessao_morta=False)
    voz = FakeVoz()
    ex = FakeExecutor(boom=True)
    estado = {}
    acao = captura.coordenar(_proj(), ac, voz, executor=ex, curso_url=URL,
                             estado=estado, agora=AGORA)
    assert acao.escalar and not acao.executada
    assert estado.get("fase") != captura.FASE_CAPTURANDO
    assert voz.avisos == [] and len(voz.escaladas) == 1


def test_disparo_sem_confirmacao_escala_honesto():
    ac = FakeAcesso(sessao_morta=False)
    voz = FakeVoz()
    ex = FakeExecutor(confirmacao="")
    estado = {}
    acao = captura.coordenar(_proj(), ac, voz, executor=ex, curso_url=URL,
                             estado=estado, agora=AGORA)
    assert acao.escalar and not acao.executada
    assert estado.get("fase") != captura.FASE_CAPTURANDO


def test_disparo_via_fila_com_banco_off_escala_honesto():
    # Sessão desconhecida -> segue -> FilaExecutor tenta o INSERT, banco off -> escala.
    ac = FakeAcesso(sessao_morta=False, boom_sql=True)
    voz = FakeVoz()
    ex = captura.FilaExecutor(ac, _proj())
    estado = {}
    acao = captura.coordenar(_proj(), ac, voz, executor=ex, curso_url=URL,
                             estado=estado, agora=AGORA)
    assert acao.escalar and not acao.executada
    assert estado.get("fase") != captura.FASE_CAPTURANDO


# ==========================================================================
# FASE 3 — monitorar progresso
# ==========================================================================
def test_capturando_incompleto_fica_quieto_sem_ingest():
    ac = FakeAcesso(resolve="55", aulas=["no_notion"] * 4 + ["pendente"] * 6)
    voz = FakeVoz()
    estado = {"fase": captura.FASE_CAPTURANDO}
    acao = captura.coordenar(_proj(), ac, voz, executor=FakeExecutor(),
                             curso_url=URL, estado=estado, agora=AGORA)
    assert acao is None                            # nada a reportar (não spamma)
    assert ac.execs == []                          # NÃO disparou auto-ingest
    assert estado["fase"] == captura.FASE_CAPTURANDO


def test_fila_inacessivel_no_monitor_escala_honesto():
    ac = FakeAcesso(boom_sql=True)
    voz = FakeVoz()
    estado = {"fase": captura.FASE_CAPTURANDO}
    acao = captura.coordenar(_proj(), ac, voz, executor=FakeExecutor(),
                             curso_url=URL, estado=estado, agora=AGORA)
    assert acao.escalar and not acao.executada
    assert len(voz.escaladas) == 1


def test_progresso_inacessivel_escala_honesto():
    # course_id resolve, mas a query de estado_aulas falha -> escala progresso.
    ac = FakeAcesso(resolve="55", boom_aulas=True)
    voz = FakeVoz()
    estado = {"fase": captura.FASE_CAPTURANDO}
    acao = captura.coordenar(_proj(), ac, voz, executor=FakeExecutor(),
                             curso_url=URL, estado=estado, agora=AGORA)
    assert acao.escalar and not acao.executada
    assert len(voz.escaladas) == 1


# ==========================================================================
# FASE 4 — concluído -> auto-ingest reusando conhecimento.reconciliar
# ==========================================================================
def test_curso_completo_dispara_autoingest_via_reconciliar():
    ac = FakeAcesso(resolve="55", aulas=["no_notion"] * 10,
                    reconcile="RECONCILE_OK {'ingeridas': 10}")
    voz = FakeVoz()
    estado = {"fase": captura.FASE_CAPTURANDO}
    acao = captura.coordenar(_proj(), ac, voz, executor=FakeExecutor(),
                             curso_url=URL, estado=estado, agora=AGORA)
    assert acao.executada and not acao.escalar
    assert ac.execs == [("conhecimentoinfinito_conhecimentoinfinito",
                         "python -m conhecimento.reconcile")]
    assert estado["fase"] == captura.FASE_CONCLUIDO
    assert any("reconcile" in a.descricao for a in voz.avisos)


def test_autoingest_que_falha_escala_e_nao_marca_concluido():
    ac = FakeAcesso(resolve="55", aulas=["no_notion"] * 10, boom_app=True)
    voz = FakeVoz()
    estado = {"fase": captura.FASE_CAPTURANDO}
    acao = captura.coordenar(_proj(), ac, voz, executor=FakeExecutor(),
                             curso_url=URL, estado=estado, agora=AGORA)
    assert acao.escalar and not acao.executada
    assert estado["fase"] != captura.FASE_CONCLUIDO


def test_autoingest_sem_sentinela_escala_nao_finge_sucesso():
    ac = FakeAcesso(resolve="55", aulas=["no_notion"] * 10, reconcile="Traceback: boom")
    voz = FakeVoz()
    estado = {"fase": captura.FASE_CAPTURANDO}
    acao = captura.coordenar(_proj(), ac, voz, executor=FakeExecutor(),
                             curso_url=URL, estado=estado, agora=AGORA)
    assert acao.escalar and not acao.executada
    assert estado["fase"] != captura.FASE_CONCLUIDO


def test_autoingest_sem_app_container_escala():
    ac = FakeAcesso(resolve="55", aulas=["no_notion"] * 10)
    voz = FakeVoz()
    estado = {"fase": captura.FASE_CAPTURANDO}
    acao = captura.coordenar(_proj(app_container=""), ac, voz,
                             executor=FakeExecutor(), curso_url=URL,
                             estado=estado, agora=AGORA)
    assert acao.escalar and not acao.executada
    assert ac.execs == []


def test_concluido_nao_redispara_nada():
    ac = FakeAcesso(sessao_morta=False, resolve="55", aulas=["no_notion"] * 10)
    voz = FakeVoz()
    ex = FakeExecutor()
    estado = {"fase": captura.FASE_CONCLUIDO}
    acao = captura.coordenar(_proj(), ac, voz, executor=ex, curso_url=URL,
                             estado=estado, agora=AGORA)
    assert acao is None
    assert ex.disparos == [] and ac.execs == []    # idempotente e quieto


# ==========================================================================
# INVIOLÁVEL estrutural: o adaptador NUNCA importa um driver de browser
# ==========================================================================
def test_adaptador_nao_importa_browser():
    import inspect
    baixo = inspect.getsource(captura).lower()
    assert "playwright" not in baixo
    assert "selenium" not in baixo
    assert "webdriver" not in baixo


# ==========================================================================
# Protocolo completo ao longo de ciclos (estado persiste)
# ==========================================================================
def test_protocolo_completo_novo_ate_concluido():
    voz = FakeVoz()
    ex = FakeExecutor(confirmacao="enfileirado:42")
    estado = {}

    # ciclo 1: sem sinal de morte -> dispara por URL
    a1 = captura.coordenar(_proj(), FakeAcesso(sessao_morta=False), voz, executor=ex,
                           curso_url=URL, estado=estado, agora=AGORA)
    assert a1.executada and estado["fase"] == captura.FASE_CAPTURANDO
    assert ex.disparos == [URL]

    # ciclo 2: worker ainda não reivindicou (sem course_id) -> aguarda, quieto
    a2 = captura.coordenar(_proj(), FakeAcesso(resolve=""), voz, executor=ex,
                           curso_url=URL, estado=estado, agora=AGORA)
    assert a2 is None

    # ciclo 3: reivindicado (course_id) mas ainda capturando -> quieto
    a3 = captura.coordenar(_proj(), FakeAcesso(resolve="777", aulas=["no_notion", "pendente"]),
                           voz, executor=ex, curso_url=URL, estado=estado, agora=AGORA)
    assert a3 is None and ex.disparos == [URL]     # disparou UMA vez só

    # ciclo 4: completou -> auto-ingest -> concluído
    ac4 = FakeAcesso(resolve="777", aulas=["no_notion", "no_notion"], reconcile="RECONCILE_OK {}")
    a4 = captura.coordenar(_proj(), ac4, voz, executor=ex, curso_url=URL,
                           estado=estado, agora=AGORA)
    assert a4.executada and estado["fase"] == captura.FASE_CONCLUIDO
    assert ac4.execs and ac4.execs[0][1] == "python -m conhecimento.reconcile"


# ==========================================================================
# SEAM da esteira (classificador/sintetizador) — declarado, não implementado
# ==========================================================================
def test_seam_esteira_existe_e_e_noop():
    assert hasattr(captura, "_hooks_esteira")
    assert captura._hooks_esteira(_proj(), URL, {}) == []
