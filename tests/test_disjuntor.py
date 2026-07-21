"""Disjuntor com RECOZIMENTO (P4 — o coração do never-stop).

O disjuntor antigo (athena_local.py:113-121) era um TETO PERMANENTE: ao bater
`tentativas >= max_tentativas` (3), parava de disparar PARA SEMPRE até um humano
mexer. Num sistema 24/7 sem-parada isso é o oposto do que se quer: uma falha
transitória (a plataforma caiu 5min, a sessão expirou) condena o curso à parada
eterna.

O disjuntor novo é RE-ARMÁVEL: ao bater o limiar, entra numa janela de backoff
exponencial (10min -> 1h -> 6h, teto 24h). Quando a janela vence, ele SE RE-ARMA
sozinho e libera UMA nova tentativa. Só vira parada permanente quando um
classificador externo (causa.classificar, P1) marca `st['irredutivel']` — porque
aí a causa é irredutível (curso deletado, acesso revogado) e martelar é inútil.

Interface pura e injetável: o relógio entra como parâmetro `agora` (float, epoch),
o estado é o dict por-curso `st` já existente. Sem I/O, sem time.time() escondido.
"""
import maestro.disjuntor as d


# --------------------------------------------------------------------------- #
# Antes de qualquer falha, o disjuntor está fechado (deixa passar).
def test_estado_virgem_pode_tentar():
    st = {}
    assert d.pode_tentar(st, agora=1000.0) is True


# As primeiras `limiar` falhas são "de graça" (não bloqueiam) — igual ao antigo,
# que só parava AO atingir o teto. O disjuntor só arma a janela NO limiar.
def test_falhas_abaixo_do_limiar_nao_bloqueiam():
    st = {}
    d.registrar_falha(st, agora=0.0)          # 1ª
    assert d.pode_tentar(st, agora=1.0) is True
    d.registrar_falha(st, agora=1.0)          # 2ª
    assert d.pode_tentar(st, agora=2.0) is True


# --------------------------------------------------------------------------- #
# O CICLO COMPLETO com relógio injetado (critério de pronto):
# 3 falhas -> bloqueado -> avança 601s -> pode_tentar True de novo -> sucesso zera.
def test_ciclo_completo_rearm_com_relogio_injetado():
    st = {}
    t = 0.0
    d.registrar_falha(st, agora=t)            # 1ª
    d.registrar_falha(st, agora=t)            # 2ª
    d.registrar_falha(st, agora=t)            # 3ª -> ARMA a 1ª janela (600s)

    # bloqueado logo depois e durante toda a janela de 600s (espera MÍNIMA de 600s)
    assert d.pode_tentar(st, agora=t) is False
    assert d.pode_tentar(st, agora=t + 599.0) is False
    assert d.pode_tentar(st, agora=t + 599.999) is False  # ainda dentro da janela

    # a espera mínima de 600s cumpriu-se -> RE-ARMA sozinho -> libera nova tentativa.
    # (o critério de pronto avança 601s; a fronteira exata em 600s também já libera.)
    assert d.pode_tentar(st, agora=t + 600.0) is True
    assert d.pode_tentar(st, agora=t + 601.0) is True

    # a nova tentativa deu certo -> sucesso ZERA tudo (volta ao estado virgem)
    d.registrar_sucesso(st)
    assert d.pode_tentar(st, agora=t + 602.0) is True
    assert st.get("disj_falhas", 0) == 0


# --------------------------------------------------------------------------- #
# Backoff EXPONENCIAL re-armável: 600 -> 3600 -> 21600, teto 86400.
# Cada falha ALÉM do limiar sobe um degrau da escada.
def test_backoff_exponencial_sobe_degraus():
    st = {}
    for _ in range(3):
        d.registrar_falha(st, agora=0.0)      # 3ª arma janela de 600s
    assert d.pode_tentar(st, agora=599.999) is False
    assert d.pode_tentar(st, agora=601.0) is True

    d.registrar_falha(st, agora=601.0)        # 4ª -> janela de 3600s
    assert d.pode_tentar(st, agora=601.0 + 3599.0) is False
    assert d.pode_tentar(st, agora=601.0 + 3601.0) is True

    d.registrar_falha(st, agora=5000.0)       # 5ª -> janela de 21600s
    assert d.pode_tentar(st, agora=5000.0 + 21599.0) is False
    assert d.pode_tentar(st, agora=5000.0 + 21601.0) is True


def test_teto_do_backoff_satura_em_86400():
    st = {}
    for _ in range(3):
        d.registrar_falha(st, agora=0.0)      # limiar
    # muitas falhas além do limiar: a janela satura no teto de 86400s (24h)
    for _ in range(20):
        d.registrar_falha(st, agora=0.0)
    assert d.pode_tentar(st, agora=86399.0) is False
    assert d.pode_tentar(st, agora=86401.0) is True


# --------------------------------------------------------------------------- #
# Sucesso re-arma DE VERDADE: depois de escalar até 6h, um sucesso volta tudo
# ao degrau zero (a próxima batelada de falhas recomeça em 600s, não em 6h).
def test_sucesso_recomeça_a_escada_do_zero():
    st = {}
    for _ in range(5):
        d.registrar_falha(st, agora=0.0)      # já no 3º degrau (21600s)
    d.registrar_sucesso(st)
    # nova batelada: as 2 primeiras são de graça, a 3ª arma 600s (não 21600s)
    for _ in range(3):
        d.registrar_falha(st, agora=100000.0)
    assert d.pode_tentar(st, agora=100000.0 + 599.0) is False
    assert d.pode_tentar(st, agora=100000.0 + 601.0) is True


# --------------------------------------------------------------------------- #
# PARADA PERMANENTE só quando irredutível: causa.classificar (P1) marca a flag.
def test_irredutivel_e_parada_permanente_mesmo_sem_falhas_do_disjuntor():
    st = {"irredutivel": True}
    # nem o tempo nem a ausência de falhas liberam: irredutível é definitivo
    assert d.pode_tentar(st, agora=0.0) is False
    assert d.pode_tentar(st, agora=10**12) is False


def test_irredutivel_vence_uma_janela_que_ja_teria_rearmado():
    st = {}
    for _ in range(3):
        d.registrar_falha(st, agora=0.0)
    # a janela venceria em 601s (re-armaria), MAS foi classificado irredutível:
    st["irredutivel"] = True
    assert d.pode_tentar(st, agora=601.0) is False


# --------------------------------------------------------------------------- #
# TESTE DE REGRESSÃO COM DENTES.
#
# Reintroduz DE PROPÓSITO o comportamento antigo (parada permanente ao bater o
# limiar) e prova que, no CENÁRIO DO RE-ARM, ele daria a resposta ERRADA (False).
# Isso mostra que `test_ciclo_completo_rearm_com_relogio_injetado` NÃO é
# decoração: se alguém reverter `pode_tentar` para o teto permanente, aquele
# teste FALHA — porque a resposta certa (True após a janela) exige o recozimento.
def _pode_tentar_ANTIGO_teto_permanente(st, agora, limiar=3):
    """Cópia fiel do disjuntor antigo (athena_local.py:113): >= limiar => para
    de disparar PARA SEMPRE. Não olha o relógio, não re-arma."""
    return st.get("disj_falhas", 0) < limiar


def test_regressao_o_teto_antigo_falharia_o_cenario_de_rearm():
    st = {}
    t = 0.0
    for _ in range(3):
        d.registrar_falha(st, agora=t)        # mesmo setup do ciclo completo

    # No instante t+601 (janela vencida), as duas implementações DIVERGEM:
    #   - a NOVA re-arma  -> True   (o never-stop volta a disparar)
    #   - a ANTIGA trava  -> False  (parada permanente)
    assert d.pode_tentar(st, agora=t + 601.0) is True
    assert _pode_tentar_ANTIGO_teto_permanente(st, agora=t + 601.0) is False

    # E a prova de que o teste discrimina: se `d.pode_tentar` FOSSE o teto antigo,
    # este assert seria idêntico ao de cima e o teste do re-arm quebraria.
    assert d.pode_tentar(st, agora=t + 601.0) != _pode_tentar_ANTIGO_teto_permanente(
        st, agora=t + 601.0)


# --------------------------------------------------------------------------- #
# PUREZA: nada de relógio escondido. Duas chamadas com o MESMO `agora` são
# idempotentes na leitura, e o módulo nunca chama time.time() por baixo.
def test_pode_tentar_e_pura_no_relogio_injetado():
    st = {}
    for _ in range(3):
        d.registrar_falha(st, agora=0.0)
    # o congelamento é total: o veredito depende SÓ do `agora` passado
    assert d.pode_tentar(st, agora=599.999) is False
    assert d.pode_tentar(st, agora=600.001) is True
    assert d.pode_tentar(st, agora=599.999) is False   # sem efeito colateral


def test_registrar_sucesso_preserva_irredutivel():
    # sucesso zera o backoff, mas NÃO desfaz uma classificação de irredutível —
    # só o classificador (ou um humano) desmarca uma causa raiz irredutível.
    st = {"irredutivel": True}
    for _ in range(3):
        d.registrar_falha(st, agora=0.0)
    d.registrar_sucesso(st)
    assert st.get("disj_falhas", 0) == 0
    assert st["irredutivel"] is True
    assert d.pode_tentar(st, agora=10**9) is False
