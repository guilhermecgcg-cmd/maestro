"""VISÃO-DE-PROGRESSO + AUDITOR (dono: Athena).

A Athena tem de ENXERGAR o progresso REAL da captura na FONTE DE VERDADE (Notion),
e REJEITAR o falso-pronto: um curso/job só é "concluído" quando o Notion PROVA, nunca
por um flag. Este é o gate I-1 — nasceu de "pronto" declarado com 466 aulas pendentes.

Os dublês MODELAM O MECANISMO, não a coreografia:

  - FakeNotionApp.exec_app REPRODUZ o container do app: faz o MESMO shlex-parse que o
    `sh -c` faria no argv, extrai o PREFIXO passado, e aplica o MESMO `starts_with` que
    o filtro de URL do Notion aplica sobre a propriedade "Origem". Assim, se o código
    esquecer de normalizar a URL (tirar a query string), o prefixo carrega '?...' e
    NENHUMA aula casa -> contagem 0 -> completo dá errado [TEETH da normalização].

  - progresso_curso_no_notion devolve a CONTAGEM real (no_notion) daquele curso; o
    `total` (esperado) vem da enumeração, injetado — a Athena NÃO enumera aqui.
"""
import shlex

import pytest

from maestro.adaptadores import captura
from maestro import observador, auditor
from maestro.registro import Projeto


AGORA = 1_000_000.0
CURSO = "https://hotmart.com/pt-br/marketplace/produtos/x/products/3486759"
# aulas reais: a URL da aula COMEÇA pela URL do curso + '/content/...'
AULAS = [CURSO + "/content/AAA", CURSO + "/content/BBB", CURSO + "/content/CCC"]
ALVO = "conhecimentoinfinito_conhecimentoinfinito"


def _proj(**kw):
    base = dict(nome="captura", projeto_easypanel="conhecimentoinfinito",
                servicos=("worker",), adaptador="captura",
                db_container="conhecimentoinfinito_db", db_name="conhecimento",
                db_user="postgres", app_container=ALVO, gerenciar=True)
    base.update(kw)
    return Projeto(**base)


class FakeNotionApp:
    """Modela o container do app: exec_app re-parseia o argv como o `sh -c` faria e
    aplica o MESMO starts_with que o filtro de URL do Notion aplica sobre 'Origem'.
    Serve tanto o script anti-dup (JA_NO_NOTION) quanto o de progresso (PROGRESSO_NOTION):
    imprime a sentinela que o comando pediu, com a contagem por prefixo."""
    def __init__(self, lessons=(), boom=False, saida=None):
        self.lessons = list(lessons)
        self.boom = boom
        self.saida = saida
        self.comandos = []

    def exec_app(self, container, comando, timeout=None):
        self.comandos.append((container, comando, timeout))
        if self.boom:
            raise RuntimeError("docker off")
        if self.saida is not None:
            return self.saida
        prefixo = shlex.split(comando)[-1]          # último token do argv = o PREFIXO
        n = sum(1 for u in self.lessons if u.startswith(prefixo))
        sent = (captura.PROGRESSO_SENTINELA if captura.PROGRESSO_SENTINELA in comando
                else captura.NOTION_SENTINELA)
        return f"{sent} {n}\n"

    # Em produção o `acesso` do observador e o do exec_app são o MESMO objeto (a
    # Acesso única). Modelamos ambos os papéis aqui: serviços/recursos vazios (o teste
    # só se importa com est.progresso).
    def servicos(self):
        return {}

    def recursos(self):
        return {"disco_pct": 0.0, "ram_pct": 0.0}

    def saude_http(self, alvos):
        return {}


class FakeVoz:
    def __init__(self):
        self.avisos = []
        self.escaladas = []

    def avisar_acao(self, acao):
        self.avisos.append(acao)

    def escalar(self, problema, pedido):
        self.escaladas.append((problema, pedido))


class FakeExecutor:
    def __init__(self, confirmacao="enfileirado:res"):
        self._confirmacao = confirmacao
        self.disparos = []

    def disparar(self, curso_url):
        self.disparos.append(curso_url)
        return self._confirmacao


def _prog(no_notion, url=CURSO, ts=AGORA):
    return captura.ProgressoNotion(course_url=url, no_notion=no_notion, ts=ts)


# ==========================================================================
# 1 + 2 — progresso_curso_no_notion + ProgressoNotion.completo
# ==========================================================================
def test_progresso_conta_aulas_no_notion_por_prefixo():
    ac = FakeNotionApp(lessons=AULAS)
    p = captura.progresso_curso_no_notion(ac, ALVO, CURSO)
    assert isinstance(p, captura.ProgressoNotion)
    assert p.no_notion == 3 and p.course_url == CURSO
    # alcançou o Notion DE DENTRO do container do app (reusa o token do app).
    cont, comando, _ = ac.comandos[0]
    assert cont == ALVO
    assert "notion_client" in comando and captura.PROGRESSO_SENTINELA in comando


def test_progresso_curso_novo_conta_zero():
    ac = FakeNotionApp(lessons=[])
    assert captura.progresso_curso_no_notion(ac, ALVO, CURSO).no_notion == 0


def test_completo_so_quando_no_notion_alcanca_total():
    assert _prog(18).completo(18) is True
    assert _prog(18).completo(10) is True          # excedeu o esperado ainda é completo
    assert _prog(10).completo(18) is False         # FALSO-PRONTO: 10/18
    assert _prog(0).completo(0) is False           # total 0 nunca prova conclusão
    assert _prog(5).completo(0) is False


def test_progresso_sem_sentinela_levanta_honesto():
    # Saída sem a sentinela (ex.: traceback) NÃO pode virar 0 silencioso -> LEVANTA.
    ac = FakeNotionApp(saida="Traceback (most recent call last): boom")
    with pytest.raises(Exception):
        captura.progresso_curso_no_notion(ac, ALVO, CURSO)


def test_progresso_sem_alvo_container_levanta():
    with pytest.raises(Exception):
        captura.progresso_curso_no_notion(FakeNotionApp(), "", CURSO)


# ---- TEETH (b): normalização do prefixo de 'Origem' -----------------------
def test_count_match_apesar_de_query_string_na_url():
    # A URL cadastrada pode vir com '?access_source=...'; as aulas NÃO têm essa query.
    # Normalizar (tirar a query) ANTES do prefixo é o que faz o count casar. Se o
    # código não normalizar, o prefixo carrega '?...' e nada casa -> no_notion 0 ->
    # completo(3) vira False. Este é o TEETH da normalização.
    ac = FakeNotionApp(lessons=AULAS)
    p = captura.progresso_curso_no_notion(ac, ALVO, CURSO + "?access_source=hero&utm=x")
    assert p.no_notion == 3
    assert p.completo(3) is True


def test_count_nao_da_falso_positivo_em_url_parecida():
    # Curso 3486759 vs 34867590 (um dígito a mais): sem a fronteira '/', o prefixo
    # casaria as aulas do outro curso. TEETH do prefixo.
    outro = "https://hotmart.com/pt-br/marketplace/produtos/x/products/34867590"
    ac = FakeNotionApp(lessons=[outro + "/content/Z", outro + "/content/Y"])
    assert captura.progresso_curso_no_notion(ac, ALVO, CURSO).no_notion == 0


# ==========================================================================
# 3 — wiring do seam progresso_fn do observador -> contagem REAL do Notion
# ==========================================================================
def test_observador_progresso_reflete_a_verdade_do_notion():
    ac = FakeNotionApp(lessons=AULAS)               # Notion tem 3 aulas deste curso
    prog_fn = captura.progresso_fn_observador(ac, ALVO, {CURSO: 3}, AGORA)
    est = observador.coletar_estado(ac, {}, agora=AGORA, progresso_fn=prog_fn)
    assert len(est.progresso) == 1
    pc = est.progresso[0]
    assert isinstance(pc, observador.ProgressoCurso)
    assert pc.curso == CURSO and pc.done == 3 and pc.total == 3
    assert pc.ts == AGORA
    assert pc.completo is True                       # done>=total -> completo


def test_observador_progresso_ve_curso_incompleto_como_nao_completo():
    ac = FakeNotionApp(lessons=AULAS)               # 3 no Notion, mas esperado 18
    prog_fn = captura.progresso_fn_observador(ac, ALVO, {CURSO: 18}, AGORA)
    est = observador.coletar_estado(ac, {}, agora=AGORA, progresso_fn=prog_fn)
    pc = est.progresso[0]
    assert pc.done == 3 and pc.total == 18
    assert pc.completo is False                      # FALSO-PRONTO não passa


# ==========================================================================
# 4 — auditar_conclusao: rejeita falso-pronto (gate I-1), confirma o real
# ==========================================================================
def test_auditar_conclusao_confirma_quando_notion_bate():
    laudo = auditor.auditar_conclusao("job-42", _prog(18), 18)
    assert laudo.aprovado is True


def test_auditar_conclusao_rejeita_falso_pronto_10_de_18():
    # TEETH (a): se o Auditor confirmar 'pronto' sem checar o Notion (ignorar o
    # progresso), este 10/18 passaria como completo. O gate DEVE reprovar.
    laudo = auditor.auditar_conclusao("job-42", _prog(10), 18)
    assert laudo.aprovado is False
    ev = " ".join(r.evidencia for r in laudo.resultados)
    assert "10/18" in ev and "8 faltando" in ev     # gap honesto: reportado X/Y, Z faltando


def test_auditar_conclusao_total_zero_reprova():
    # total 0 (enumeração vazia) nunca prova conclusão -> fail-closed.
    assert auditor.auditar_conclusao("job", _prog(0), 0).aprovado is False


def test_auditar_conclusao_escala_via_voz_no_falso_pronto():
    voz = FakeVoz()
    laudo = auditor.auditar_conclusao("job-42", _prog(10, url=CURSO), 18, voz=voz)
    assert laudo.aprovado is False
    assert len(voz.escaladas) == 1
    _problema, pedido = voz.escaladas[0]
    assert "10/18" in pedido and "8 faltando" in pedido
    assert _problema.tipo == "falso_pronto"


def test_auditar_conclusao_confirmado_nao_escala():
    voz = FakeVoz()
    auditor.auditar_conclusao("job-42", _prog(18), 18, voz=voz)
    assert voz.escaladas == []


# ==========================================================================
# 5 — INTEGRAÇÃO: coordenar só marca CONCLUIDO se o Notion PROVAR (gate I-1)
# ==========================================================================
class FakeAcessoTracker:
    """Tracker (estado_aulas) diz 'done' pelo course_id; NÃO fala com o Notion. O
    gate de conclusão (auditar por Notion) é injetado à parte via progresso_notion_fn."""
    def __init__(self, *, resolve, aulas, reconcile="RECONCILE_OK {}"):
        self._resolve = resolve
        self._aulas = aulas
        self._reconcile = reconcile
        self.execs = []

    def exec_sql(self, container, sql, *, db, user="postgres", rows=True):
        up = sql.upper()
        if "ESTADO_AULAS" in up:
            total = len(self._aulas)
            # todas terminais -> pend 0 (o tracker diz "done")
            return [f"{total}|0"]
        if "FILA_CAPTURA" in up and captura.STATUS_SESSAO_MORTA.upper() in up:
            return []
        if "FILA_CAPTURA" in up:
            return [self._resolve] if self._resolve else []
        return []

    def exec_app(self, container, comando, timeout=None):
        self.execs.append((container, comando, timeout))
        return self._reconcile


def test_integracao_gate_rejeita_falso_pronto_nao_marca_concluido():
    # Tracker diz done (10 aulas terminais), MAS o Notion só tem 8/10 -> falso-pronto.
    ac = FakeAcessoTracker(resolve="777", aulas=["no_notion"] * 10)
    voz = FakeVoz()
    estado = {"fase": captura.FASE_CAPTURANDO}
    prog_notion_fn = lambda url: _prog(8, url=url)  # Notion prova só 8 de 10
    acao = captura.coordenar(_proj(), ac, voz, executor=FakeExecutor(),
                             curso_url=CURSO, estado=estado, agora=AGORA,
                             progresso_notion_fn=prog_notion_fn)
    assert estado["fase"] != captura.FASE_CONCLUIDO   # NÃO declarou concluído
    assert acao is not None and acao.escalar
    assert ac.execs == []                             # nem rodou o ingest do falso-pronto
    assert len(voz.escaladas) == 1
    assert "8/10" in voz.escaladas[0][1]


def test_integracao_gate_confirma_quando_notion_prova_conclui():
    ac = FakeAcessoTracker(resolve="777", aulas=["no_notion"] * 10)
    voz = FakeVoz()
    estado = {"fase": captura.FASE_CAPTURANDO}
    prog_notion_fn = lambda url: _prog(10, url=url)  # Notion prova os 10
    acao = captura.coordenar(_proj(), ac, voz, executor=FakeExecutor(),
                             curso_url=CURSO, estado=estado, agora=AGORA,
                             progresso_notion_fn=prog_notion_fn)
    assert estado["fase"] == captura.FASE_CONCLUIDO
    assert acao.executada and not acao.escalar
    assert ac.execs                                   # rodou o auto-ingest


def test_integracao_sem_seam_mantem_comportamento_antigo():
    # Retrocompatível: sem o gate injetado, conclui pelo tracker como antes (as suítes
    # existentes não passam progresso_notion_fn e continuam verdes).
    ac = FakeAcessoTracker(resolve="777", aulas=["no_notion"] * 10)
    voz = FakeVoz()
    estado = {"fase": captura.FASE_CAPTURANDO}
    acao = captura.coordenar(_proj(), ac, voz, executor=FakeExecutor(),
                             curso_url=CURSO, estado=estado, agora=AGORA)
    assert estado["fase"] == captura.FASE_CONCLUIDO
    assert acao.executada
