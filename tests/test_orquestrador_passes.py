"""CABEÇA DE CAPTURA — orquestra as PASSADAS por curso até 100% MEDIDO NO NOTION,
com o DISJUNTOR que separa 'parede real anti-ban' de 'aula não-vídeo'.

Contexto (a dor que isto resolve): um curso raramente fica pronto numa passada só.
A captura tem 4 passadas em cascata sobre a MESMA lista de aulas:

  1. legenda  (WebVTT)          — a captura-base (a existente `coordenar` dispara)
  2. áudio    (Whisper)         — para aulas que ficaram `sem_legenda`
  3. embed    (Vimeo/HLS)       — para aulas que ficaram `sem_video`
  4. não-vídeo(documento/texto) — para aulas que ficaram `sem_embed`/`sem_audio`

O falso-pronto nasce de confundir DUAS coisas que 'não geraram vídeo':
  - PAREDE real anti-ban: a aula TEM conteúdo mas a captura foi BLOQUEADA
    (`falhou`/`audio_erro`). Contar isso como 'feito' é o falso-pronto que o usuário
    ODEIA. → NÃO conta como done, NÃO declara 100%, escala (anti-ban = decisão dele).
  - AULA não-vídeo: a aula legitimamente não tem vídeo (documento/texto). Contar isso
    como FALHA faria o curso nunca chegar a 100% e escalaria anti-ban à toa. → conta
    como done depois que a passada não-vídeo teve sua chance.

O DISJUNTOR é o classificador que separa os dois. As passadas são SEAMS injetáveis
(`disparar_passe`) — este teste NUNCA roda captura real. O censo de estados também é
seam (`censo_fn`): dublês que MODELAM O MECANISMO (estados reais do tracker), com
TEETH — se o mapa der `falhou`→feito, o falso-pronto passa; se der `sem_embed`→parede,
o não-vídeo trava para sempre.
"""
import pytest

from maestro.adaptadores import orquestrador as orq
from maestro.registro import Projeto


AGORA = 1_000_000.0
CURSO = "https://hotmart.com/pt-br/marketplace/produtos/x/products/3486759"


def _proj(**kw):
    base = dict(nome="captura", projeto_easypanel="conhecimentoinfinito",
                servicos=("worker",), adaptador="captura",
                db_container="conhecimentoinfinito_db", db_name="conhecimento",
                db_user="postgres",
                app_container="conhecimentoinfinito_conhecimentoinfinito",
                gerenciar=True)
    base.update(kw)
    return Projeto(**base)


class FakeVoz:
    def __init__(self):
        self.avisos = []
        self.escaladas = []

    def avisar_acao(self, acao):
        self.avisos.append(acao)

    def escalar(self, problema, pedido):
        self.escaladas.append((problema, pedido))


class FakeDisparo:
    """Seam de passada: (passe, curso_url) -> confirmação truthy. Registra o que
    disparou, na ORDEM. Modela o executor residencial que enfileira a passada — não
    abre Chrome, não roda captura de verdade."""
    def __init__(self, confirmacao="enfileirado:passe"):
        self._conf = confirmacao
        self.disparos = []

    def __call__(self, passe, curso_url):
        self.disparos.append((passe, curso_url))
        return self._conf


def _censo(mapa):
    """Fábrica de censo_fn: (curso_url) -> dict[estado, contagem]."""
    return lambda url: dict(mapa)


# ==========================================================================
# 1 — classificar (o coração do DISJUNTOR): estado do tracker -> classe
# ==========================================================================
def test_classificar_estados_capturados_sao_em_notion():
    # no_notion = literalmente NO NOTION (a medida de verdade). anexos_baixados idem.
    assert orq.classificar("no_notion")[0] == orq.CLASSE_CAPTURADA
    assert orq.classificar("anexos_baixados")[0] == orq.CLASSE_CAPTURADA


def test_classificar_falhas_reais_sao_parede():
    # TEETH do falso-pronto: falhou/audio_erro NUNCA podem cair em 'feito'. São PAREDE.
    assert orq.classificar("falhou")[0] == orq.CLASSE_PAREDE
    assert orq.classificar("audio_erro")[0] == orq.CLASSE_PAREDE


def test_classificar_sem_x_roteia_para_a_passada_certa():
    # cada 'sem X' benigno aponta a passada que o resolve, na cascata.
    assert orq.classificar("sem_legenda")[1] == orq.PASSE_AUDIO
    assert orq.classificar("sem_video")[1] == orq.PASSE_EMBED
    assert orq.classificar("sem_embed")[1] == orq.PASSE_NAO_VIDEO
    assert orq.classificar("sem_audio")[1] == orq.PASSE_NAO_VIDEO


def test_classificar_estado_desconhecido_ou_em_voo_nunca_vira_feito():
    # não-terminal/desconhecido = captura-base em voo (ou estado novo). NUNCA CAPTURADA
    # (senão vira falso-pronto). Fica PENDENTE, não-benigno (bloqueia, não é disparável).
    for est in ("pendente", "capturando", "baixando", "estado_novo_qualquer"):
        classe, passe, benigno = orq.classificar(est)
        assert classe == orq.CLASSE_PENDENTE
        assert benigno is False
    # transcrevendo_embed é a passada de embed TRABALHANDO -> em voo, não 'feito'.
    classe, _, benigno = orq.classificar("transcrevendo_embed")
    assert classe == orq.CLASSE_PENDENTE and benigno is False


# ==========================================================================
# 2 — diagnosticar: o censo por-estado vira o retrato do curso
# ==========================================================================
def test_diagnostico_tudo_no_notion_e_completo():
    d = orq.diagnosticar({"no_notion": 18})
    assert d.total == 18 and d.capturadas == 18
    assert d.paredes == 0 and not d.pendentes
    assert d.proximo_passe is None
    assert d.completo is True


def test_diagnostico_proxima_passada_respeita_a_cascata():
    # sem_video (embed) + sem_legenda (audio), nenhuma passada disparada ainda:
    # a PRÓXIMA é a mais cedo na ordem legenda->audio->embed->nao_video => audio.
    d = orq.diagnosticar({"no_notion": 5, "sem_legenda": 2, "sem_video": 1})
    assert d.proximo_passe == orq.PASSE_AUDIO
    assert d.completo is False


def test_diagnostico_parede_bloqueia_o_completo():
    # TEETH (falso-pronto): 17 no Notion + 1 falhou, TODAS as passadas já rodaram.
    # NÃO é 100% — há uma parede real. Se falhou fosse 'feito', completo daria True.
    d = orq.diagnosticar({"no_notion": 17, "falhou": 1},
                         passes_disparados=(orq.PASSE_AUDIO, orq.PASSE_EMBED,
                                            orq.PASSE_NAO_VIDEO))
    assert d.paredes == 1
    assert d.completo is False


def test_diagnostico_nao_video_conta_como_feito_apos_passada_exaurir():
    # TEETH (falso-negativo): sem_embed depois que a passada nao_video já rodou = aula
    # legitimamente não-vídeo, tratada -> conta como done, curso chega a 100%.
    d = orq.diagnosticar({"no_notion": 10, "sem_embed": 2},
                         passes_disparados=(orq.PASSE_NAO_VIDEO,))
    assert d.nao_video == 2 and d.paredes == 0
    assert not d.pendentes
    assert d.completo is True


def test_diagnostico_sem_x_pendente_ate_a_passada_ter_sua_chance():
    # sem_embed ANTES da passada nao_video rodar = PENDENTE (dispare a passada),
    # ainda NÃO conta como done. Sem isto, pularíamos a passada não-vídeo.
    d = orq.diagnosticar({"no_notion": 10, "sem_embed": 2})
    assert d.nao_video == 0
    assert d.proximo_passe == orq.PASSE_NAO_VIDEO
    assert d.completo is False


def test_diagnostico_total_zero_nunca_e_completo():
    # censo vazio (curso não enumerado ainda) NUNCA prova conclusão (fail-closed).
    d = orq.diagnosticar({})
    assert d.total == 0
    assert d.completo is False


def test_diagnostico_captura_base_em_voo_bloqueia_e_nao_dispara_passada():
    # aulas ainda em estado não-terminal = captura-base (legenda) rodando. Bloqueia o
    # completo, mas a cabeça NÃO dispara a passada de legenda (quem dispara é coordenar).
    d = orq.diagnosticar({"no_notion": 3, "pendente": 2})
    assert d.completo is False
    assert d.proximo_passe is None       # não fura a captura-base
    assert d.em_voo == 2


# ==========================================================================
# 3 — orquestrar (passo por ciclo): dispara passada / escala parede / conclui
# ==========================================================================
def test_orquestrar_dispara_a_proxima_passada_e_registra_no_estado():
    voz = FakeVoz()
    disparo = FakeDisparo()
    estado = {}
    censo = _censo({"no_notion": 5, "sem_legenda": 3})
    acao = orq.orquestrar(_proj(), voz, censo_fn=censo, disparar_passe=disparo,
                          curso_url=CURSO, estado=estado, agora=AGORA)
    assert disparo.disparos == [(orq.PASSE_AUDIO, CURSO)]
    assert acao.executada and not acao.escalar
    assert orq.PASSE_AUDIO in estado["passes_disparados"]
    assert len(voz.avisos) == 1


def test_orquestrar_cascata_ordena_audio_embed_naovideo():
    # A MESMA cabeça, ciclos sucessivos, com um censo que AVANÇA conforme as passadas
    # rodam (modela o mecanismo). Deve disparar audio -> embed -> nao_video, nessa
    # ordem, uma por ciclo, e então declarar 100%.
    voz = FakeVoz()
    disparo = FakeDisparo()
    estado = {}
    # sequência de censos, um por ciclo (o que a passada anterior 'resolveu').
    sequencia = [
        {"no_notion": 5, "sem_legenda": 3, "sem_video": 2, "sem_embed": 1},  # dispara audio
        {"no_notion": 7, "sem_video": 2, "sem_embed": 1},                    # audio resolveu 2; dispara embed
        {"no_notion": 9, "sem_embed": 1},                                    # embed resolveu 2; dispara nao_video
        {"no_notion": 9, "anexos_baixados": 1},                              # nao_video resolveu -> 100%
    ]
    caixa = {"i": 0}

    def censo(url):
        return dict(sequencia[caixa["i"]])

    for passo in range(4):
        caixa["i"] = passo
        orq.orquestrar(_proj(), voz, censo_fn=censo, disparar_passe=disparo,
                       curso_url=CURSO, estado=estado, agora=AGORA)
    assert [p for p, _ in disparo.disparos] == [orq.PASSE_AUDIO, orq.PASSE_EMBED,
                                                orq.PASSE_NAO_VIDEO]
    assert estado.get("orq_fase") == orq.ORQ_CONCLUIDO


def test_orquestrar_parede_escala_anti_ban_e_nao_declara_pronto():
    # TEETH: com uma parede real, a cabeça NÃO pode declarar 100% (falso-pronto). Escala
    # anti-ban (decisão do humano) e NÃO dispara mais passadas (não piora o ban).
    voz = FakeVoz()
    disparo = FakeDisparo()
    estado = {}
    censo = _censo({"no_notion": 17, "falhou": 1})
    acao = orq.orquestrar(_proj(), voz, censo_fn=censo, disparar_passe=disparo,
                          curso_url=CURSO, estado=estado, agora=AGORA)
    assert acao.escalar and not acao.executada
    assert estado.get("orq_fase") != orq.ORQ_CONCLUIDO
    assert disparo.disparos == []                       # não dispara nada com parede acesa
    assert len(voz.escaladas) == 1
    prob, _ = voz.escaladas[0]
    assert prob.tipo == "parede_anti_ban"


def test_orquestrar_parede_nao_spamma_ciclo_a_ciclo():
    # A mesma parede em dois ciclos escala UMA vez (não spamma o Telegram).
    voz = FakeVoz()
    disparo = FakeDisparo()
    estado = {}
    censo = _censo({"no_notion": 17, "falhou": 1})
    orq.orquestrar(_proj(), voz, censo_fn=censo, disparar_passe=disparo,
                   curso_url=CURSO, estado=estado, agora=AGORA)
    orq.orquestrar(_proj(), voz, censo_fn=censo, disparar_passe=disparo,
                   curso_url=CURSO, estado=estado, agora=AGORA)
    assert len(voz.escaladas) == 1


def test_orquestrar_declara_100pct_so_com_a_verdade_do_notion():
    voz = FakeVoz()
    disparo = FakeDisparo()
    estado = {}
    censo = _censo({"no_notion": 18})
    acao = orq.orquestrar(_proj(), voz, censo_fn=censo, disparar_passe=disparo,
                          curso_url=CURSO, estado=estado, agora=AGORA)
    assert acao.executada and not acao.escalar
    assert estado["orq_fase"] == orq.ORQ_CONCLUIDO
    assert disparo.disparos == []                       # nada a disparar: já 100%
    # idempotente: segundo ciclo não faz nada.
    a2 = orq.orquestrar(_proj(), voz, censo_fn=censo, disparar_passe=disparo,
                        curso_url=CURSO, estado=estado, agora=AGORA)
    assert a2 is None
    assert len(voz.avisos) == 1


def test_orquestrar_nao_video_chega_a_100_sem_falso_negativo():
    # TEETH (o outro lado do disjuntor): um curso com aulas legitimamente não-vídeo. Depois
    # que a passada nao_video roda e as aulas seguem 'sem_embed' (não há vídeo mesmo), a
    # cabeça declara 100% — não trava para sempre nem escala anti-ban.
    voz = FakeVoz()
    disparo = FakeDisparo()
    estado = {}
    seq = [
        {"no_notion": 8, "sem_embed": 2},   # dispara nao_video
        {"no_notion": 8, "sem_embed": 2},   # nao_video exauriu; segue não-vídeo -> feito
    ]
    caixa = {"i": 0}
    censo = lambda url: dict(seq[caixa["i"]])
    caixa["i"] = 0
    a1 = orq.orquestrar(_proj(), voz, censo_fn=censo, disparar_passe=disparo,
                        curso_url=CURSO, estado=estado, agora=AGORA)
    assert disparo.disparos == [(orq.PASSE_NAO_VIDEO, CURSO)]
    assert not a1.escalar
    caixa["i"] = 1
    a2 = orq.orquestrar(_proj(), voz, censo_fn=censo, disparar_passe=disparo,
                        curso_url=CURSO, estado=estado, agora=AGORA)
    assert a2.executada and estado["orq_fase"] == orq.ORQ_CONCLUIDO
    assert voz.escaladas == []                          # não-vídeo NÃO é anti-ban


def test_orquestrar_falha_ao_disparar_passada_escala_e_nao_marca_disparada():
    # disparar_passe levanta -> escala honesto e NÃO registra a passada como disparada
    # (o próximo ciclo re-tenta). Nunca finge sucesso.
    def disparo_boom(passe, url):
        raise RuntimeError("fila off")
    voz = FakeVoz()
    estado = {}
    censo = _censo({"no_notion": 5, "sem_legenda": 3})
    acao = orq.orquestrar(_proj(), voz, censo_fn=censo, disparar_passe=disparo_boom,
                          curso_url=CURSO, estado=estado, agora=AGORA)
    assert acao.escalar and not acao.executada
    assert orq.PASSE_AUDIO not in estado.get("passes_disparados", [])
    assert len(voz.escaladas) == 1


def test_orquestrar_passada_sem_confirmacao_escala_e_nao_marca():
    # confirmação falsy do executor -> não assume sucesso; escala; não marca disparada.
    voz = FakeVoz()
    estado = {}
    censo = _censo({"no_notion": 5, "sem_legenda": 3})
    acao = orq.orquestrar(_proj(), voz, censo_fn=censo,
                          disparar_passe=FakeDisparo(confirmacao=""),
                          curso_url=CURSO, estado=estado, agora=AGORA)
    assert acao.escalar and not acao.executada
    assert orq.PASSE_AUDIO not in estado.get("passes_disparados", [])


def test_orquestrar_espera_quando_captura_base_em_voo():
    # aulas ainda não-terminais = captura-base rodando. A cabeça AGUARDA: nada dispara,
    # nada declara. Sem barulho.
    voz = FakeVoz()
    disparo = FakeDisparo()
    estado = {}
    censo = _censo({"no_notion": 3, "pendente": 5})
    acao = orq.orquestrar(_proj(), voz, censo_fn=censo, disparar_passe=disparo,
                          curso_url=CURSO, estado=estado, agora=AGORA)
    assert acao is None
    assert disparo.disparos == []
    assert estado.get("orq_fase") != orq.ORQ_CONCLUIDO
    assert voz.avisos == [] and voz.escaladas == []


def test_orquestrar_censo_inacessivel_escala_nao_declara():
    # se não dá para ler o censo, o lado seguro é NÃO declarar nada e escalar honesto.
    def censo_boom(url):
        raise RuntimeError("db off")
    voz = FakeVoz()
    estado = {}
    acao = orq.orquestrar(_proj(), voz, censo_fn=censo_boom,
                          disparar_passe=FakeDisparo(),
                          curso_url=CURSO, estado=estado, agora=AGORA)
    assert acao.escalar and not acao.executada
    assert estado.get("orq_fase") != orq.ORQ_CONCLUIDO
    assert len(voz.escaladas) == 1


def test_orquestrar_parede_que_some_destrava_o_reporte():
    # parede reportada num ciclo; se num ciclo seguinte ela some (reseed resolveu) e o
    # curso completa, a trava de 'já reportei' não pode impedir a conclusão.
    voz = FakeVoz()
    disparo = FakeDisparo()
    estado = {}
    seq = [{"no_notion": 17, "falhou": 1}, {"no_notion": 18}]
    caixa = {"i": 0}
    censo = lambda url: dict(seq[caixa["i"]])
    caixa["i"] = 0
    orq.orquestrar(_proj(), voz, censo_fn=censo, disparar_passe=disparo,
                   curso_url=CURSO, estado=estado, agora=AGORA)
    caixa["i"] = 1
    a2 = orq.orquestrar(_proj(), voz, censo_fn=censo, disparar_passe=disparo,
                        curso_url=CURSO, estado=estado, agora=AGORA)
    assert a2.executada and estado["orq_fase"] == orq.ORQ_CONCLUIDO


# ==========================================================================
# 4 — censo_estados: o seam REAL de leitura do tracker (agrupado por status)
# ==========================================================================
class FakeAcessoCenso:
    """Modela o tracker: exec_sql de um GROUP BY status devolve linhas 'status|n'."""
    def __init__(self, linhas):
        self._linhas = linhas
        self.sqls = []

    def exec_sql(self, container, sql, *, db, user="postgres", rows=True):
        self.sqls.append(sql)
        return list(self._linhas)


def test_censo_estados_agrupa_por_status():
    ac = FakeAcessoCenso(["no_notion|10", "sem_legenda|3", "falhou|1"])
    censo = orq.censo_estados(_proj(), ac, "cid-777")
    assert censo == {"no_notion": 10, "sem_legenda": 3, "falhou": 1}
    # consultou estado_aulas por course_id (quotado).
    assert "estado_aulas" in ac.sqls[0].lower()
    assert "cid-777" in ac.sqls[0]


def test_censo_estados_sem_linhas_vira_vazio():
    assert orq.censo_estados(_proj(), FakeAcessoCenso([]), "cid") == {}


def test_censo_estados_ignora_linha_malformada():
    # linha sem o separador não derruba o censo (robustez); a boa é lida.
    ac = FakeAcessoCenso(["no_notion|5", "lixo-sem-pipe", "sem_video|2"])
    assert orq.censo_estados(_proj(), ac, "cid") == {"no_notion": 5, "sem_video": 2}
