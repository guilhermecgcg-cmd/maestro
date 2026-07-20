"""Capacidade A — Portão POP automatizado (a Athena roda o gate de qualidade
SOZINHA). Dado um artefato novo, ela dispara em loop: review -> corrige TODOS os
bugs (não só graves) -> review, até ZERAR, e só então declara pronto.

O que estes testes exercem é a LÓGICA DO LOOP — não o review nem o fix reais.
Review/verificação/fix são COSTURAS injetáveis (callables); os dublês aqui MODELAM
o mecanismo (rodadas de review roteiradas, correção com/sem dentes, falso-positivo)
para que as regras invioláveis fiquem provadas:

  1. não avança enquanto sobrar bug confirmado (fail-closed);
  2. TODOS os bugs bloqueiam — cosmético reprova igual a grave;
  3. teste-com-dentes obrigatório — fix cujo teste não falha contra o código antigo
     é decoração, e NÃO deixa o portão passar;
  4. verificar cada achado ANTES de corrigir — falso-positivo não é corrigido nem
     bloqueia;
  5. só um review LIMPO (zero achados) declara pronto — re-revisa após cada fix.

Os testes de 'dentes' (marcados) foram provados: reintroduzi o bug no loop e
confirmei que cada um FALHA contra a implementação ingênua correspondente."""
from maestro import portao_pop
from maestro.portao_pop import Achado, Correcao


# --- Dublês que MODELAM o mecanismo ----------------------------------------
class RevisorRoteirado:
    """Modela o agente de review: cada RODADA devolve uma lista de achados,
    roteirada de fora. Registra quantas vezes foi chamado (pra provar re-review)."""
    def __init__(self, rodadas):
        self._rodadas = list(rodadas)
        self.chamadas = 0
        self.artefatos_vistos = []

    def __call__(self, artefato):
        self.artefatos_vistos.append(artefato)
        i = self.chamadas
        self.chamadas += 1
        if i < len(self._rodadas):
            return list(self._rodadas[i])
        return []  # depois do roteiro, review limpo


class RevisorDeterministico:
    """Modela um agente de review REAL: sempre devolve os MESMOS achados enquanto
    o artefato não muda (review é função do código). Ao contrário do Roteirado,
    NÃO 'esvazia' sozinho — se ninguém corrigiu nada, a próxima volta acha o
    mesmo. É o dublê com dentes pro caso do falso-positivo teimoso."""
    def __init__(self, achados):
        self._achados = list(achados)
        self.chamadas = 0

    def __call__(self, artefato):
        self.chamadas += 1
        return list(self._achados)


class Verificador:
    """Modela 'verificar o achado contra o código antes de implementar'. IDs em
    `falsos_positivos` são descartados (review errou). Registra o que verificou."""
    def __init__(self, falsos_positivos=()):
        self._fp = set(falsos_positivos)
        self.verificados = []

    def __call__(self, achado, artefato):
        self.verificados.append(achado.id)
        return achado.id not in self._fp


class Corretor:
    """Modela o agente de fix. Para cada achado devolve uma Correcao configurável
    (aplicada? teste com dentes?). Registra QUAIS achados tentou corrigir — é o
    spy que prova que falso-positivo NUNCA chega ao fix."""
    def __init__(self, *, aplicada=True, com_dentes=True, por_id=None):
        self._aplicada = aplicada
        self._com_dentes = com_dentes
        self._por_id = dict(por_id or {})
        self.corrigidos = []

    def __call__(self, achado, artefato):
        self.corrigidos.append(achado.id)
        aplicada, dentes = self._por_id.get(
            achado.id, (self._aplicada, self._com_dentes))
        return Correcao(achado_id=achado.id, aplicada=aplicada,
                        teste_com_dentes=dentes)


def _achado(id, sev="grave"):
    return Achado(id=id, descricao=f"bug {id}", severidade=sev)


# --- Caminho feliz: review limpo de cara -> pronto imediato ----------------
def test_review_limpo_de_cara_declara_pronto():
    rev = RevisorRoteirado([[]])  # nenhum achado
    res = portao_pop.rodar_portao(
        "artefato", revisar=rev, verificar=Verificador(), corrigir=Corretor())
    assert res.pronto is True
    assert res.escalar is False


# --- Não avança com bug pendente: fix não aplicado -------------------------
def test_bug_confirmado_sem_fix_aplicado_nao_declara_pronto():
    rev = RevisorRoteirado([[_achado("A")]])
    corr = Corretor(aplicada=False)  # o fix falhou
    res = portao_pop.rodar_portao(
        "art", revisar=rev, verificar=Verificador(), corrigir=corr)
    assert res.pronto is False
    assert res.escalar is True


# --- DENTES: fix sem teste-com-dentes é decoração, não deixa passar --------
def test_dentes_fix_sem_teste_com_dentes_nao_deixa_passar():
    """Teste-com-dentes: reintroduzi o bug de IGNORAR o flag `teste_com_dentes`
    (loop que declara pronto assim que 'todos os fixes foram aplicados'). Contra
    esse loop ingênuo este teste FALHA — logo, tem dentes."""
    rev = RevisorRoteirado([[_achado("A")]])
    corr = Corretor(aplicada=True, com_dentes=False)  # fix aplicado MAS sem dentes
    res = portao_pop.rodar_portao(
        "art", revisar=rev, verificar=Verificador(), corrigir=corr)
    assert res.pronto is False
    assert res.escalar is True


# --- TODOS os bugs bloqueiam: cosmético reprova igual a grave ---------------
def test_dentes_cosmetico_bloqueia_igual_a_grave():
    """Teste-com-dentes: reintroduzi 'só graves bloqueiam' (pendente só quando
    severidade=='grave'). Um achado COSMÉTICO sem dentes passaria nesse loop
    bugado; aqui exigimos que reprove. Logo, tem dentes."""
    rev = RevisorRoteirado([[_achado("A", sev="cosmetico")]])
    corr = Corretor(com_dentes=False)  # cosmético não corrigido de verdade
    res = portao_pop.rodar_portao(
        "art", revisar=rev, verificar=Verificador(), corrigir=corr)
    assert res.pronto is False
    assert res.escalar is True


# --- Falso-positivo: verificado, descartado, NUNCA corrigido, NÃO bloqueia --
def test_dentes_falso_positivo_nao_e_corrigido_nem_bloqueia():
    """Teste-com-dentes: reintroduzi 'corrige sem verificar' (chama o fix pra todo
    achado). Nesse loop o falso-positivo iria ao Corretor e/ou bloquearia; aqui
    exigimos que o Corretor NÃO seja chamado pra ele e que o portão passe."""
    rev = RevisorRoteirado([[_achado("FP")]])  # depois disso, review limpo
    verif = Verificador(falsos_positivos={"FP"})
    corr = Corretor()
    res = portao_pop.rodar_portao(
        "art", revisar=rev, verificar=verif, corrigir=corr)
    assert "FP" in verif.verificados          # foi verificado
    assert "FP" not in corr.corrigidos        # NÃO foi corrigido (era falso-positivo)
    assert res.pronto is True                 # não bloqueou


# --- DENTES: falso-positivo TEIMOSO (review determinístico) -> pronto, não escala
def test_dentes_falso_positivo_teimoso_converge_pronto_sem_escalar():
    """Teste-com-dentes: reintroduzi 'só um review VAZIO declara pronto' (o loop
    re-revisa após TODA rodada, mesmo quando nada foi corrigido). Um review REAL é
    determinístico: se o achado é falso-positivo e ninguém mexeu no código, ele
    reaparece em TODA rodada. Contra o loop ingênuo isto esgota o orçamento e
    ESCALA por engano — com o artefato limpo. Exigimos: rodada sem NENHUM bug
    confirmado (tudo falso-positivo) declara pronto na hora, não chama o fix, e
    não desperdiça re-reviews. Logo, tem dentes."""
    rev = RevisorDeterministico([_achado("FP")])  # reaparece toda rodada
    verif = Verificador(falsos_positivos={"FP"})
    corr = Corretor()
    res = portao_pop.rodar_portao(
        "art", revisar=rev, verificar=verif, corrigir=corr, max_rodadas=10)
    assert res.pronto is True        # zero bugs confirmados = pronto
    assert res.escalar is False      # NÃO escala um artefato limpo
    assert corr.corrigidos == []     # falso-positivo nunca vai ao fix
    assert rev.chamadas == 1         # convergiu já na 1ª rodada (sem re-review inútil)


# --- DENTES: bug real corrigido + falso-positivo teimoso na mesma esteira ---
def test_dentes_bug_corrigido_e_fp_teimoso_convergem():
    """Um review determinístico que sempre acusa REAL (some após corrigido) e FP
    (falso-positivo, nunca some). Rodada 1: corrige REAL com dentes -> artefato
    muda -> re-revisa. Rodada 2: review determinístico ainda acusa FP (só ele);
    zero confirmados -> pronto. O loop ingênuo (re-revisa sempre) ficaria preso no
    FP até escalar. Exigimos convergência para pronto."""
    class RevSomeReal:
        def __init__(self):
            self.chamadas = 0
        def __call__(self, artefato):
            self.chamadas += 1
            return [_achado("REAL"), _achado("FP")] if self.chamadas == 1 else [_achado("FP")]
    rev = RevSomeReal()
    verif = Verificador(falsos_positivos={"FP"})
    corr = Corretor(aplicada=True, com_dentes=True)
    res = portao_pop.rodar_portao(
        "art", revisar=rev, verificar=verif, corrigir=corr, max_rodadas=10)
    assert res.pronto is True
    assert res.escalar is False
    assert corr.corrigidos == ["REAL"]   # só o confirmado foi ao fix
    assert rev.chamadas == 2             # 1 review + 1 re-review (após o fix real)


# --- Verificar ANTES de corrigir, e só corrigir o confirmado ---------------
def test_verifica_todo_achado_e_so_corrige_confirmado():
    rev = RevisorRoteirado([[_achado("REAL"), _achado("FP")]])
    verif = Verificador(falsos_positivos={"FP"})
    corr = Corretor()
    portao_pop.rodar_portao("art", revisar=rev, verificar=verif, corrigir=corr)
    assert set(verif.verificados) >= {"REAL", "FP"}  # verificou os dois
    assert corr.corrigidos == ["REAL"]               # corrigiu SÓ o confirmado


# --- Só review LIMPO declara pronto: re-revisa após cada fix ---------------
def test_dentes_re_revisa_apos_fix_ate_review_limpo():
    """Teste-com-dentes: reintroduzi 'declara pronto assim que corrigiu os achados
    da rodada' (sem re-revisar). A rodada 2 do review revela um bug NOVO (que o
    fix da rodada 1 introduziu); o loop ingênuo teria dito pronto na rodada 1 e
    perdido o bug da 2. Aqui exigimos pronto SÓ na rodada limpa."""
    rev = RevisorRoteirado([
        [_achado("A")],   # rodada 1: um bug
        [_achado("B")],   # rodada 2 (re-review): fix da 1 revelou/introduziu B
        [],               # rodada 3: limpo
    ])
    corr = Corretor(com_dentes=True, aplicada=True)
    res = portao_pop.rodar_portao(
        "art", revisar=rev, verificar=Verificador(), corrigir=corr)
    assert res.pronto is True
    assert corr.corrigidos == ["A", "B"]   # corrigiu os dois, em rodadas distintas
    assert rev.chamadas == 3               # revisou 3x (re-review real)
    assert len(res.rodadas) == 3


# --- Fail-closed: review nunca limpa dentro do orçamento de rodadas --------
def test_dentes_fail_closed_esgota_rodadas_sem_declarar_pronto():
    """Teste-com-dentes: reintroduzi 'declara pronto ao esgotar as rodadas' (sair
    do laço com pronto=True). Aqui o review SEMPRE acha um bug novo (nunca limpa);
    exigimos que o portão NÃO minta 'pronto' e escale."""
    # cada rodada sempre acha um novo bug, mesmo corrigido com dentes
    rev = RevisorRoteirado([[_achado(f"R{i}")] for i in range(50)])
    corr = Corretor(com_dentes=True, aplicada=True)
    res = portao_pop.rodar_portao(
        "art", revisar=rev, verificar=Verificador(), corrigir=corr, max_rodadas=5)
    assert res.pronto is False
    assert res.escalar is True
    assert rev.chamadas == 5   # respeitou o orçamento


# --- Fail-closed degenerado: orçamento de rodadas < 1 ----------------------
def test_dentes_max_rodadas_zero_nao_declara_pronto_vazio():
    """Teste-com-dentes: reintroduzi 'pronto vacuamente' (nenhuma rodada rodou ->
    'não achei bug' -> pronto). Sem revisar NADA não se pode declarar pronto."""
    rev = RevisorRoteirado([])
    res = portao_pop.rodar_portao(
        "art", revisar=rev, verificar=Verificador(), corrigir=Corretor(),
        max_rodadas=0)
    assert res.pronto is False
    assert res.escalar is True
    assert rev.chamadas == 0   # não revisou nada -> não pode afirmar pronto


# --- Histórico auditável por rodada ----------------------------------------
def test_historico_registra_cada_rodada_com_achados_e_correcoes():
    rev = RevisorRoteirado([[_achado("A"), _achado("FP")], []])
    verif = Verificador(falsos_positivos={"FP"})
    res = portao_pop.rodar_portao(
        "art", revisar=rev, verificar=verif, corrigir=Corretor())
    assert res.pronto is True
    r1 = res.rodadas[0]
    assert r1.n == 1
    assert set(r1.confirmados) == {"A"}
    assert set(r1.descartados) == {"FP"}
    assert [c.achado_id for c in r1.correcoes] == ["A"]
