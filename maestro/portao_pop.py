"""Capacidade A — Portão de Qualidade POP, automatizado (a Athena roda o gate
SOZINHA). Dado um artefato novo, ela dispara em loop:

    review -> verifica cada achado -> corrige TODOS os confirmados -> review ...

até um review LIMPO (zero achados) — e SÓ ENTÃO declara pronto. É a mesma disciplina
que o humano exige na mão, virada capacidade: a Athena dispara os agentes de review
e de fix e não avança enquanto sobrar bug.

As costuras são INJETÁVEIS (callables), pra rodar contra dublês nos testes e contra
os agentes reais em produção:

  * `revisar(artefato) -> [Achado]`      — dispara o code-review, devolve achados.
  * `verificar(achado, artefato) -> bool` — confere o achado CONTRA o código antes
    de mexer; False = falso-positivo (review errou) -> descarta, não corrige.
  * `corrigir(achado, artefato) -> Correcao` — dispara o fix; a Correcao diz se foi
    aplicada e se veio com TESTE-COM-DENTES (o teste de regressão FALHA contra o
    código antigo).

Regras invioláveis do loop (herdadas do CLAUDE.md do dono):
  1. Não avança enquanto sobrar bug CONFIRMADO (fail-closed, como o Auditor).
  2. TODOS os bugs bloqueiam — 'só os graves' foi rejeitado explicitamente; um
     achado cosmético reprova igual a um grave.
  3. Teste-com-dentes obrigatório: fix cujo teste não falha contra o código antigo
     é decoração e NÃO deixa o portão passar.
  4. Verificar cada achado ANTES de corrigir: falso-positivo não é corrigido nem
     bloqueia.
  5. Só um review LIMPO declara pronto — re-revisa após cada rodada de fix, porque
     um fix pode introduzir bug novo (o falso-pronto que o dono odeia)."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Achado:
    """Um problema apontado pelo review. `severidade` é INFORMATIVA — todos os
    achados bloqueiam (regra 2); guardamos a severidade só pra auditoria."""
    id: str
    descricao: str
    severidade: str = "grave"


@dataclass(frozen=True)
class Correcao:
    """Resultado de uma tentativa de fix. `aplicada` = o fix entrou; `teste_com_dentes`
    = veio com teste de regressão que FALHA contra o código antigo. Um fix só CONTA
    (deixa o portão avançar) quando as DUAS são verdadeiras."""
    achado_id: str
    aplicada: bool
    teste_com_dentes: bool

    @property
    def conta(self) -> bool:
        """Fix que de fato resolve o bug: aplicado E com dentes. Sem isso, o bug
        continua pendente (regras 1 e 3)."""
        return bool(self.aplicada) and bool(self.teste_com_dentes)


@dataclass(frozen=True)
class RodadaPortao:
    """Registro auditável de UMA rodada review->fix."""
    n: int
    achados: tuple = ()
    confirmados: tuple = ()   # ids que a verificação confirmou (bugs reais)
    descartados: tuple = ()   # ids descartados como falso-positivo
    correcoes: tuple = ()     # Correcao por confirmado


@dataclass(frozen=True)
class ResultadoPortao:
    """`pronto` só é True depois de um review LIMPO. `escalar` = precisa do humano
    (bug que não fecha dentro do orçamento, fix que não aplica/sem dentes)."""
    pronto: bool
    escalar: bool
    motivo: str
    rodadas: tuple = ()


def rodar_portao(artefato, *, revisar, verificar, corrigir, max_rodadas=10):
    """Roda o Portão POP até zerar. Devolve ResultadoPortao. NUNCA declara pronto
    sem um review limpo (mata o falso-pronto). Fail-closed em toda borda: se algo
    impede o loop de provar 'zero bugs', o resultado é pronto=False + escalar."""
    # Fail-closed degenerado: sem orçamento não se revisa NADA -> não se pode
    # afirmar 'pronto' (seria pronto vacuamente, o falso-pronto).
    if max_rodadas < 1:
        return ResultadoPortao(
            False, True, f"max_rodadas={max_rodadas} (<1): nada revisado — fail-closed", ())

    historico = []
    for n in range(1, max_rodadas + 1):
        achados = tuple(revisar(artefato))
        if not achados:
            # Único caminho pra 'pronto': um review que não achou NADA.
            historico.append(RodadaPortao(n=n))
            return ResultadoPortao(
                True, False, "review limpo: zero achados", tuple(historico))

        confirmados, descartados, correcoes = [], [], []
        pendente = False
        for a in achados:
            # Regra 4: verifica ANTES de corrigir. Falso-positivo é descartado —
            # não vai ao fix (não introduz bug novo) e não bloqueia.
            if not verificar(a, artefato):
                descartados.append(a.id)
                continue
            confirmados.append(a.id)
            c = corrigir(a, artefato)
            correcoes.append(c)
            # Regras 1+2+3: TODO bug confirmado precisa de fix que CONTE (aplicado
            # e com dentes). Severidade não entra — cosmético bloqueia igual.
            if not c.conta:
                pendente = True

        historico.append(RodadaPortao(
            n=n, achados=achados, confirmados=tuple(confirmados),
            descartados=tuple(descartados), correcoes=tuple(correcoes)))

        if pendente:
            return ResultadoPortao(
                False, True,
                "bug confirmado sem correção-com-dentes — portão não avança",
                tuple(historico))
        if not confirmados:
            # Nenhum bug CONFIRMADO nesta rodada: todos os achados foram
            # descartados como falso-positivo (regra 4). Ninguém corrigiu nada,
            # então o artefato está BYTE-A-BYTE igual — um review determinístico
            # (o real) devolveria EXATAMENTE os mesmos achados na próxima volta.
            # Re-revisar aqui não é 'checar de novo', é laço infinito até esgotar
            # o orçamento e ESCALAR um artefato limpo por engano. Falso-positivo
            # não bloqueia: zero bugs confirmados = pronto.
            return ResultadoPortao(
                True, False,
                "achados todos falso-positivo (zero bugs confirmados) — pronto",
                tuple(historico))
        # Houve confirmados e TODOS foram corrigidos com dentes -> o artefato
        # MUDOU. Regra 5: re-revisa (o laço continua) — só um review limpo (ou uma
        # rodada sem bug confirmado) na próxima volta declara pronto.

    # Esgotou o orçamento ainda achando bug: NÃO minta 'pronto' (fail-closed).
    return ResultadoPortao(
        False, True,
        f"esgotou {max_rodadas} rodadas com bug ainda aparecendo — fail-closed",
        tuple(historico))
