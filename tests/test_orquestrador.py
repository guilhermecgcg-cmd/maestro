"""Camada 2 — ORQUESTRADOR (o CÉREBRO da Athena) com FEEDBACK LOOP (P3 da spec).

O que esta camada faz, e por que ela existe:

  Dado o ESTADO OBSERVADO (vindo do observador da Camada 1 — costura injetável) e o
  ESTADO ESPERADO de cada serviço/curso, a Athena:
    1. DIAGNOSTICA as divergências (o que está fora do esperado).
    2. DECIDE uma ação da WHITELIST {restart, redeploy, reenqueue, disparar_passada,
       escalar} — nada fora dela age.
    3. EXECUTA a ação (costura de ações injetável).
    4. **VERIFICA o resultado contra a FONTE DE VERDADE** (Notion p/ captura,
       observador p/ serviços — costura injetável), NUNCA o output do instante.
    5. sucesso confirmado -> registra e segue; NÃO confirmado -> reconhece a falha
       (não declara sucesso), re-tenta e, esgotadas as tentativas, RE-ESCALA.

O bug que este loop mata (P3): um agente cego confia no output do INSTANTE — a ação
respondeu "OK", logo "deu certo". Mas o processo pode cair 5s depois, ou a captura
pode ter rodado sem escrever no Notion. Sem confirmar na fonte de verdade, o agente
declara falso-sucesso e nunca corrige rota.

DUBLÊS QUE MODELAM O MECANISMO (não a coreografia):

  MundoFake é a ÚNICA fonte de verdade. As AÇÕES mutam este store (ou NÃO, se o mundo
  está 'quebrado'); a VERIFICAÇÃO lê o MESMO store. Toda ação devolve, além do efeito,
  um output do INSTANTE sempre-truthy ("Restarted OK", "deploy started", ...) — de
  propósito: se o orquestrador confiasse nesse retorno em vez de reler a fonte de
  verdade, os TEETH abaixo (mundo quebrado que responde "OK") passariam como sucesso.
"""
from collections import namedtuple
from types import SimpleNamespace

import pytest

from maestro import orquestrador


# --- formas do estado observado (espelham observador.ServicoObservado/ProgressoCurso)
Serv = namedtuple("Serv", "nome up health")
Prog = namedtuple("Prog", "curso done total")

CURSO = "https://hotmart.com/pt-br/marketplace/produtos/x/products/3486759"


def estado(servicos=(), progresso=(), fila_travada=()):
    return SimpleNamespace(servicos=list(servicos), progresso=list(progresso),
                           fila_travada=list(fila_travada))


class MundoFake:
    """O mundo real: a fonte de verdade. Ações mutam o store; verificação o relê."""

    def __init__(self, *, servicos=None, cursos=None, filas=None,
                 restart_conserta=True, redeploy_conserta=True, captura_conclui=True,
                 fila_destrava=True, conserta_na_tentativa=None):
        self.servicos = dict(servicos or {})    # nome -> up(bool)  [fonte: observador]
        self.cursos = dict(cursos or {})         # url  -> [done, total]  [fonte: Notion]
        self.filas = dict(filas or {})           # alvo -> travado(bool)
        self.restart_conserta = restart_conserta
        self.redeploy_conserta = redeploy_conserta
        self.captura_conclui = captura_conclui
        self.fila_destrava = fila_destrava
        self.conserta_na_tentativa = conserta_na_tentativa  # int|None -> flaky
        self._n = {}
        self.chamadas = []

    def _ok(self, alvo, base):
        """Mundo flaky: só 'conserta' a partir da N-ésima tentativa — prova que o
        loop REALMENTE re-tenta e re-verifica, não confirma na primeira à toa."""
        if self.conserta_na_tentativa is None:
            return base
        self._n[alvo] = self._n.get(alvo, 0) + 1
        return self._n[alvo] >= self.conserta_na_tentativa

    # ---- AÇÕES da whitelist — output do INSTANTE sempre truthy ----
    def restart(self, alvo):
        self.chamadas.append(("restart", alvo))
        if self._ok(alvo, self.restart_conserta):
            self.servicos[alvo] = True
        return f"Restarted {alvo} — OK"

    def redeploy(self, alvo):
        self.chamadas.append(("redeploy", alvo))
        if self._ok(alvo, self.redeploy_conserta):
            self.servicos[alvo] = True
        return {"deploy": "started", "ok": True}

    def reenqueue(self, alvo):
        self.chamadas.append(("reenqueue", alvo))
        if self._ok(alvo, self.fila_destrava):
            self.filas[alvo] = False
        return f"enfileirado:{alvo}"

    def disparar_passada(self, alvo):
        self.chamadas.append(("disparar_passada", alvo))
        if self._ok(alvo, self.captura_conclui):
            _done, total = self.cursos.get(alvo, [0, 0])
            self.cursos[alvo] = [total, total]
        return f"passada disparada:{alvo}"

    # ---- FONTE DE VERDADE ----
    def servico_up(self, alvo):
        return bool(self.servicos.get(alvo, False))

    def curso_done_total(self, alvo):
        return tuple(self.cursos.get(alvo, (0, 0)))

    def fila_travada(self, alvo):
        return bool(self.filas.get(alvo, False))


def make_verificar(mundo):
    """Costura de VERIFICAÇÃO: relê a FONTE DE VERDADE p/ o tipo de divergência.
    É o mesmo store que as ações mutam — então reflete o EFEITO real da ação."""
    def verificar(div):
        if div.tipo in ("servico_caido", "servico_doente"):
            return mundo.servico_up(div.alvo)
        if div.tipo == "curso_incompleto":
            done, total = mundo.curso_done_total(div.alvo)
            return total > 0 and done >= total
        if div.tipo == "fila_travada":
            return not mundo.fila_travada(div.alvo)
        return False
    return verificar


class FakeVoz:
    def __init__(self):
        self.avisos = []
        self.escaladas = []

    def avisar_acao(self, acao):
        self.avisos.append(acao)

    def escalar(self, problema, pedido):
        self.escaladas.append((problema, pedido))


def _div(tipo, alvo):
    return orquestrador.Divergencia(tipo=tipo, alvo=alvo, detalhe="", esperado="", observado="")


# =====================================================================
# 1 — decidir: mapeia divergência -> ação da WHITELIST (fail-closed)
# =====================================================================
def test_decidir_servico_caido_reinicia():
    assert orquestrador.decidir(_div("servico_caido", "worker")) == "restart"


def test_decidir_servico_doente_redeploya():
    assert orquestrador.decidir(_div("servico_doente", "app")) == "redeploy"


def test_decidir_curso_incompleto_dispara_passada():
    assert orquestrador.decidir(_div("curso_incompleto", CURSO)) == "disparar_passada"


def test_decidir_fila_travada_reenfileira():
    assert orquestrador.decidir(_div("fila_travada", CURSO)) == "reenqueue"


def test_decidir_desconhecido_escala_fail_closed():
    # Divergência sem regra NUNCA vira uma ação-chute -> escala (decisão real).
    assert orquestrador.decidir(_div("qualquer_coisa_nova", "x")) == "escalar"


def test_toda_decisao_esta_na_whitelist():
    for tipo in ("servico_caido", "servico_doente", "curso_incompleto",
                 "fila_travada", "desconhecido"):
        assert orquestrador.decidir(_div(tipo, "a")) in orquestrador.ACOES_WHITELIST


# =====================================================================
# 2 — diagnosticar: observado vs esperado -> divergências
# =====================================================================
def test_diagnostica_servico_caido():
    est = estado(servicos=[Serv("worker", up=False, health=False)])
    esp = {"servicos": {"worker": {"up": True, "health": True}}}
    divs = orquestrador.diagnosticar(est, esp)
    assert [d.tipo for d in divs] == ["servico_caido"]
    assert divs[0].alvo == "worker"


def test_diagnostica_servico_doente_up_mas_sem_health():
    est = estado(servicos=[Serv("app", up=True, health=False)])
    esp = {"servicos": {"app": {"up": True, "health": True}}}
    divs = orquestrador.diagnosticar(est, esp)
    assert [d.tipo for d in divs] == ["servico_doente"]


def test_servico_saudavel_nao_gera_divergencia():
    est = estado(servicos=[Serv("app", up=True, health=True)])
    esp = {"servicos": {"app": {"up": True, "health": True}}}
    assert orquestrador.diagnosticar(est, esp) == []


def test_diagnostica_ignora_servico_fora_do_esperado():
    # Serviço que não está no 'esperado' não é responsabilidade desta orquestração.
    est = estado(servicos=[Serv("bystander", up=False, health=False)])
    assert orquestrador.diagnosticar(est, {"servicos": {}}) == []


def test_diagnostica_curso_incompleto_falso_pronto():
    # TEETH do falso-pronto: 10/18 no observado NÃO é concluído -> divergência.
    est = estado(progresso=[Prog(CURSO, done=10, total=18)])
    esp = {"cursos": {CURSO: 18}}
    divs = orquestrador.diagnosticar(est, esp)
    assert [d.tipo for d in divs] == ["curso_incompleto"]
    assert "10/18" in divs[0].detalhe


def test_curso_completo_nao_gera_divergencia():
    est = estado(progresso=[Prog(CURSO, done=18, total=18)])
    esp = {"cursos": {CURSO: 18}}
    assert orquestrador.diagnosticar(est, esp) == []


def test_curso_total_esperado_sobrepoe_o_observado():
    # Esperado (enumeração) manda: observado diz total=3, mas esperamos 18 -> incompleto.
    est = estado(progresso=[Prog(CURSO, done=3, total=3)])
    esp = {"cursos": {CURSO: 18}}
    divs = orquestrador.diagnosticar(est, esp)
    assert [d.tipo for d in divs] == ["curso_incompleto"]
    assert "3/18" in divs[0].detalhe


# =====================================================================
# 3 — resolver: o FEEDBACK LOOP (o coração da Camada 2)
# =====================================================================
def test_acao_confirmada_na_fonte_de_verdade_registra_sucesso():
    mundo = MundoFake(servicos={"worker": False}, restart_conserta=True)
    voz = FakeVoz()
    res = orquestrador.resolver(_div("servico_caido", "worker"), mundo,
                                make_verificar(mundo), voz)
    assert res.acao == "restart"
    assert res.confirmado is True
    assert res.escalou is False
    assert res.tentativas == 1
    assert voz.escaladas == []                      # sucesso NÃO escala
    assert ("restart", "worker") in mundo.chamadas


def test_acao_nao_confirmada_reconhece_falha_e_reescala_TEETH():
    # TEETH DO FEEDBACK LOOP: o mundo está QUEBRADO — restart responde "Restarted OK"
    # (output do instante, truthy) mas o serviço CONTINUA down na fonte de verdade.
    # Se o código confiasse no retorno da ação, declararia sucesso. Ele DEVE reler a
    # fonte de verdade, ver que não subiu, e RE-ESCALAR (não declarar done).
    mundo = MundoFake(servicos={"worker": False}, restart_conserta=False)
    voz = FakeVoz()
    res = orquestrador.resolver(_div("servico_caido", "worker"), mundo,
                                make_verificar(mundo), voz, max_tentativas=3)
    assert res.confirmado is False                  # NÃO declarou sucesso
    assert res.escalou is True
    assert res.tentativas == 3                       # tentou de novo antes de desistir
    assert len(voz.escaladas) == 1                   # reconheceu a falha -> escalou
    problema, pedido = voz.escaladas[0]
    assert problema.tipo == "servico_caido"
    assert "não confirm" in pedido.lower()
    # tentou a ação a cada rodada (o output truthy não abreviou o loop)
    assert mundo.chamadas.count(("restart", "worker")) == 3


def test_mundo_flaky_conserta_na_segunda_o_loop_re_tenta_e_confirma():
    # Prova que o loop RE-TENTA e RE-VERIFICA: 1ª verificação falha, 2ª confirma.
    mundo = MundoFake(servicos={"worker": False}, conserta_na_tentativa=2)
    voz = FakeVoz()
    res = orquestrador.resolver(_div("servico_caido", "worker"), mundo,
                                make_verificar(mundo), voz, max_tentativas=3)
    assert res.confirmado is True
    assert res.tentativas == 2                       # confirmou na 2ª
    assert res.escalou is False
    assert voz.escaladas == []


def test_decisao_escalar_nao_executa_nenhuma_acao():
    # Divergência desconhecida -> escala SEM tocar em nenhuma ação (não chuta).
    mundo = MundoFake()
    voz = FakeVoz()
    res = orquestrador.resolver(_div("coisa_nova", "x"), mundo,
                                make_verificar(mundo), voz)
    assert res.acao == "escalar"
    assert res.confirmado is False
    assert res.escalou is True
    assert res.tentativas == 0
    assert mundo.chamadas == []                      # NADA foi executado
    assert len(voz.escaladas) == 1


def test_acao_que_levanta_excecao_e_re_tentada_depois_escala():
    # Ação que estoura (docker off) não confirma; o loop trata como falha, re-tenta e,
    # esgotado, escala — nunca declara sucesso por causa da exceção.
    class MundoExplode(MundoFake):
        def restart(self, alvo):
            self.chamadas.append(("restart", alvo))
            raise RuntimeError("docker socket off")

    mundo = MundoExplode(servicos={"worker": False})
    voz = FakeVoz()
    res = orquestrador.resolver(_div("servico_caido", "worker"), mundo,
                                make_verificar(mundo), voz, max_tentativas=2)
    assert res.confirmado is False
    assert res.escalou is True
    assert res.tentativas == 2
    assert mundo.chamadas.count(("restart", "worker")) == 2


def test_verificar_que_levanta_e_tratado_como_nao_confirmado_e_escala_TEETH():
    # TEETH DO FAIL-CLOSED NA VERIFICAÇÃO: reler a FONTE DE VERDADE pode ESTOURAR
    # (Notion 503, docker exec sem container, socket off — exec_sql LEVANTA de
    # propósito). Uma exceção ao verificar NÃO é confirmação. O loop DEVE tratá-la
    # como falha (re-tenta e, esgotado, ESCALA), NUNCA deixar a exceção vazar de
    # `resolver` — vazar abortaria a orquestração inteira e deixaria a ação JÁ
    # executada sem veredito: o ponto cego fail-open que este loop existe p/ matar.
    mundo = MundoFake(servicos={"worker": False}, restart_conserta=True)
    voz = FakeVoz()

    def verificar_explode(div):
        raise RuntimeError("Notion 503 / docker exec off")

    res = orquestrador.resolver(_div("servico_caido", "worker"), mundo,
                                verificar_explode, voz, max_tentativas=2)
    assert res.confirmado is False                   # exceção NÃO virou sucesso
    assert res.escalou is True
    assert res.tentativas == 2                        # re-tentou antes de desistir
    assert len(voz.escaladas) == 1
    _, pedido = voz.escaladas[0]
    assert "não confirm" in pedido.lower()
    assert mundo.chamadas.count(("restart", "worker")) == 2


def test_orquestrar_verificar_que_estoura_num_alvo_nao_aborta_os_demais_TEETH():
    # A CONSEQUÊNCIA do vazamento: se reler a verdade estoura p/ UM alvo, a
    # orquestração não pode morrer e abandonar os OUTROS. O alvo problemático vira
    # falha-escalada; os demais seguem resolvidos normalmente.
    est = estado(servicos=[Serv("worker", up=False, health=False),
                           Serv("app", up=False, health=False)])
    esp = {"servicos": {"worker": {"up": True, "health": True},
                        "app": {"up": True, "health": True}}}
    mundo = MundoFake(servicos={"worker": False, "app": False}, restart_conserta=True)
    base = make_verificar(mundo)

    def verificar(div):
        if div.alvo == "worker":
            raise RuntimeError("verdade indisponível p/ worker")
        return base(div)

    voz = FakeVoz()
    resultados = orquestrador.orquestrar(est, esp, mundo, verificar, voz,
                                         max_tentativas=2)
    por_alvo = {r.divergencia.alvo: r for r in resultados}
    assert set(por_alvo) == {"worker", "app"}        # NENHUM alvo foi abandonado
    assert por_alvo["worker"].confirmado is False and por_alvo["worker"].escalou is True
    assert por_alvo["app"].confirmado is True and por_alvo["app"].escalou is False


def test_servico_doente_redeploy_confirma_pela_fonte():
    mundo = MundoFake(servicos={"app": False}, redeploy_conserta=True)
    voz = FakeVoz()
    res = orquestrador.resolver(_div("servico_doente", "app"), mundo,
                                make_verificar(mundo), voz)
    assert res.acao == "redeploy"
    assert res.confirmado is True
    assert ("redeploy", "app") in mundo.chamadas


def test_curso_incompleto_dispara_passada_confirma_no_notion():
    # captura roda e o Notion PROVA a conclusão -> confirmado.
    mundo = MundoFake(cursos={CURSO: [10, 18]}, captura_conclui=True)
    voz = FakeVoz()
    res = orquestrador.resolver(_div("curso_incompleto", CURSO), mundo,
                                make_verificar(mundo), voz)
    assert res.acao == "disparar_passada"
    assert res.confirmado is True
    assert mundo.curso_done_total(CURSO) == (18, 18)


def test_curso_falso_pronto_passada_roda_mas_notion_nao_prova_escala_TEETH():
    # TEETH FALSO-PRONTO: disparar_passada responde "passada disparada" (instante
    # truthy) MAS o Notion continua 10/18. A verdade é o Notion -> NÃO confirma ->
    # escala. Sem reler o Notion, isto viraria um falso 'concluído'.
    mundo = MundoFake(cursos={CURSO: [10, 18]}, captura_conclui=False)
    voz = FakeVoz()
    res = orquestrador.resolver(_div("curso_incompleto", CURSO), mundo,
                                make_verificar(mundo), voz, max_tentativas=2)
    assert res.confirmado is False
    assert res.escalou is True
    assert mundo.curso_done_total(CURSO) == (10, 18)  # continua incompleto — honesto
    assert len(voz.escaladas) == 1


def test_fila_travada_reenqueue_confirma_quando_destrava():
    mundo = MundoFake(filas={CURSO: True}, fila_destrava=True)
    voz = FakeVoz()
    res = orquestrador.resolver(_div("fila_travada", CURSO), mundo,
                                make_verificar(mundo), voz)
    assert res.acao == "reenqueue"
    assert res.confirmado is True


# =====================================================================
# 4 — orquestrar: integração sobre TODAS as divergências de um snapshot
# =====================================================================
def test_orquestrar_resolve_o_que_da_e_escala_o_que_nao_da():
    est = estado(
        servicos=[Serv("worker", up=False, health=False),   # cai -> restart confirma
                  Serv("app", up=True, health=False)],       # doente -> redeploy QUEBRADO
        progresso=[Prog(CURSO, done=10, total=18)],          # incompleto -> passada confirma
    )
    esp = {"servicos": {"worker": {"up": True, "health": True},
                        "app": {"up": True, "health": True}},
           "cursos": {CURSO: 18}}
    # worker sobe no restart; app NÃO sobe no redeploy (mundo quebrado p/ app); curso conclui
    mundo = MundoFake(servicos={"worker": False, "app": False},
                      cursos={CURSO: [10, 18]},
                      restart_conserta=True, redeploy_conserta=False,
                      captura_conclui=True)
    voz = FakeVoz()
    resultados = orquestrador.orquestrar(est, esp, mundo, make_verificar(mundo), voz,
                                         max_tentativas=2)
    por_alvo = {r.divergencia.alvo: r for r in resultados}
    assert por_alvo["worker"].confirmado is True and por_alvo["worker"].escalou is False
    assert por_alvo[CURSO].confirmado is True
    # o app não confirmou -> reconhecido como falha e escalado, não declarado ok
    assert por_alvo["app"].confirmado is False and por_alvo["app"].escalou is True
    assert len(voz.escaladas) == 1                   # só o app escalou


def test_orquestrar_tudo_saudavel_nao_age_nem_escala():
    est = estado(servicos=[Serv("worker", up=True, health=True)],
                 progresso=[Prog(CURSO, done=18, total=18)])
    esp = {"servicos": {"worker": {"up": True, "health": True}},
           "cursos": {CURSO: 18}}
    mundo = MundoFake()
    voz = FakeVoz()
    resultados = orquestrador.orquestrar(est, esp, mundo, make_verificar(mundo), voz)
    assert resultados == []
    assert mundo.chamadas == []
    assert voz.escaladas == []


# =====================================================================
# 5 — AcoesAthena: concretiza a whitelist sobre as costuras REAIS (Acesso+executor)
# =====================================================================
class FakeAcesso:
    def __init__(self):
        self.restarts = []
        self.redeploys = []

    def restart(self, nome):
        self.restarts.append(nome)

    def redeploy(self, nome, projeto_easypanel):
        self.redeploys.append((nome, projeto_easypanel))


class FakeExecutor:
    def __init__(self):
        self.disparos = []

    def disparar(self, curso_url):
        self.disparos.append(curso_url)
        return f"enfileirado:{curso_url}"


def _projeto():
    from maestro.registro import Projeto
    return Projeto(nome="captura", projeto_easypanel="conhecimentoinfinito",
                   servicos=("worker",))


def test_acoes_athena_restart_chama_acesso():
    ac = FakeAcesso()
    acoes = orquestrador.AcoesAthena(ac, _projeto())
    acoes.restart("worker")
    assert ac.restarts == ["worker"]


def test_acoes_athena_redeploy_usa_projeto_easypanel():
    ac = FakeAcesso()
    acoes = orquestrador.AcoesAthena(ac, _projeto())
    acoes.redeploy("app")
    assert ac.redeploys == [("app", "conhecimentoinfinito")]


def test_acoes_athena_disparar_passada_e_reenqueue_usam_executor():
    ac = FakeAcesso()
    ex = FakeExecutor()
    acoes = orquestrador.AcoesAthena(ac, _projeto(), executor=ex)
    acoes.disparar_passada(CURSO)
    acoes.reenqueue(CURSO)
    assert ex.disparos == [CURSO, CURSO]


def test_acoes_athena_sem_executor_recusa_disparo():
    # Sem executor injetado, disparar_passada/reenqueue LEVANTAM (não engolem em silêncio).
    acoes = orquestrador.AcoesAthena(FakeAcesso(), _projeto())
    with pytest.raises(Exception):
        acoes.disparar_passada(CURSO)
