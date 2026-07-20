"""Capacidade C (parte ORQUESTRAÇÃO) — a Athena OPERA o Sintetizador no fluxo
pós-captura.

Este módulo NÃO é o Sintetizador (esse já existe: o pacote `sintetizador/` do repo
do motor). É o ORQUESTRADOR que o ACIONA: dado um curso classificado como how-to
(SINAL injetável), a Athena dispara a síntese, faz o artefato gerado PASSAR pelo
Portão POP (Capacidade A) e só então o REGISTRA e declara 'sintetizado'. Curso que
não é how-to (info/vendas) não aciona nada.

Tudo é COSTURA injetável (callables), pra rodar contra dublês nos testes e contra os
sistemas reais em produção:

  * `classificar(curso) -> bool` — o SINAL how-to. No real, o classificador de
    uma-abertura; nos testes, um dublê roteirado.
  * `sintetizar(curso) -> Artefato | None` — dispara o Sintetizador; devolve o
    artefato (skill+agente+sistema) ou None se falhou.
  * `portao(artefato) -> ResultadoPortao` — roda o Portão POP no artefato (no real,
    `portao_pop.rodar_portao` já embrulhado com os agentes de review/fix). É um seam
    porque a Capacidade A vive em outro módulo/branch — o orquestrador só precisa do
    veredito `.pronto`.
  * `registrar(artefato) -> None` — registra o artefato gerado. Chamado SÓ depois
    que o portão limpou.

Invariantes (herdadas do CLAUDE.md do dono — matar o falso-pronto):
  1. how-to ACIONA a síntese; não-how-to NÃO toca síntese/portão/registro.
  2. o artefato só é registrado DEPOIS que o Portão POP limpou — nunca antes.
  3. portão reprova, síntese sem artefato, OU registro que falha -> não fica
     registrado, `sintetizado=False`, escala pro humano (fail-closed) — nenhuma
     dessas bordas pode virar crash: toda falha vira EstadoSintese auditável.
  4. `sintetizado=True` exige portão limpo E registro efetivado."""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Artefato:
    """O produto do Sintetizador pra um curso how-to: a skill destilada, o agente
    que a executa e o sistema executável. Opaco pro orquestrador — ele só o passa
    adiante (portão, registro)."""
    skill: str
    agente: str
    sistema: str


@dataclass(frozen=True)
class ResultadoPortao:
    """Veredito do Portão POP como o orquestrador precisa vê-lo. `pronto` = artefato
    aprovado (review limpo); `escalar` = precisa do humano. É estruturalmente igual
    ao ResultadoPortao da Capacidade A — duplicado aqui de propósito, pra o
    orquestrador não depender do módulo do Portão (que vive em outro branch)."""
    pronto: bool
    escalar: bool = False


@dataclass(frozen=True)
class EstadoSintese:
    """Estado auditável da orquestração de UM curso. `how_to` = o sinal; `sintetizado`
    = síntese feita E aprovada no portão E registrada; `artefato` = o gerado (mesmo
    quando reprovado, fica aqui pra auditoria); `registrado` = entrou no registro;
    `escalar` = o humano precisa decidir; `motivo` = por quê."""
    curso: object
    how_to: bool
    sintetizado: bool
    artefato: Optional[Artefato]
    registrado: bool
    escalar: bool
    motivo: str


def portao_via_pop(revisar, verificar, corrigir, *, max_rodadas=10):
    """Constrói o seam `portao` que `orquestrar_sintese` (Capacidade C) exige, DELEGANDO
    à Capacidade A (Portão de Qualidade POP, `portao_pop.rodar_portao`). É a costura que
    LIGA A a C: C não conhece o loop review->fix->review; ele só recebe um callable
    `portao(artefato) -> ResultadoPortao(pronto, escalar)`. Aqui esse callable RODA o
    Portão POP de verdade (que só devolve `pronto` após um review LIMPO) e traduz o
    veredito para a forma que C lê. Assim o artefato do Sintetizador passa OBRIGATÓRIA-
    mente pelo Portão A antes de ser registrado — a regra inviolável do dono."""
    from maestro import portao_pop

    def _portao(artefato):
        res = portao_pop.rodar_portao(artefato, revisar=revisar, verificar=verificar,
                                      corrigir=corrigir, max_rodadas=max_rodadas)
        return ResultadoPortao(pronto=res.pronto, escalar=res.escalar)

    return _portao


def orquestrar_sintese(curso, *, classificar, sintetizar, portao, registrar):
    """Opera o Sintetizador no fluxo pós-captura. Devolve EstadoSintese. Fail-closed:
    NUNCA declara 'sintetizado' sem portão limpo + registro (mata o falso-pronto)."""
    how_to = bool(classificar(curso))
    if not how_to:
        # Curso não-how-to (info/vendas): o Sintetizador NÃO se aplica. Não é falha
        # nem escalonamento — é o caminho normal. Nada é acionado.
        return EstadoSintese(
            curso=curso, how_to=False, sintetizado=False, artefato=None,
            registrado=False, escalar=False,
            motivo="curso não-how-to: Sintetizador não acionado")

    # É how-to: ACIONA o Sintetizador.
    artefato = sintetizar(curso)
    if artefato is None:
        # Síntese falhou silenciosamente (sem artefato). Não há o que revisar nem
        # registrar — fail-closed, escala.
        return EstadoSintese(
            curso=curso, how_to=True, sintetizado=False, artefato=None,
            registrado=False, escalar=True,
            motivo="Sintetizador não produziu artefato — fail-closed, escala")

    # Portão POP ANTES do registro: nunca se registra um artefato não-aprovado.
    resultado = portao(artefato)
    if not resultado.pronto:
        # Reprovou: o artefato fica no estado (auditoria) mas NÃO é registrado nem
        # declarado sintetizado. Escala (o falso-pronto que o dono odeia).
        return EstadoSintese(
            curso=curso, how_to=True, sintetizado=False, artefato=artefato,
            registrado=False, escalar=True,
            motivo="Portão POP reprovou o artefato — não registrado, escala")

    # Portão limpo: SÓ AGORA registra e declara sintetizado. O registro é I/O real
    # (Notion/DB) que FALHA na prática — e aqui já passamos do ponto sem volta (o
    # portão limpou). Uma falha crua ESCAPARIA como crash, deixando o curso num
    # limbo: portão limpo, mas sem EstadoSintese e sem escalonamento auditável — o
    # buraco exato que os outros caminhos fail-closed fecham. Fecha ele também:
    # registro que falha NÃO declara sintetizado (invariante 4: exige registro
    # efetivado), preserva o artefato pra auditoria e escala pro humano.
    try:
        registrar(artefato)
    except Exception as e:  # noqa: BLE001 — fail-closed deliberado: qualquer falha
        # do registro vira escalonamento auditável, nunca crash silencioso.
        return EstadoSintese(
            curso=curso, how_to=True, sintetizado=False, artefato=artefato,
            registrado=False, escalar=True,
            motivo=f"registro falhou após Portão POP limpo ({e!r}) — "
                   "fail-closed, escala")
    return EstadoSintese(
        curso=curso, how_to=True, sintetizado=True, artefato=artefato,
        registrado=True, escalar=False,
        motivo="sintetizado e registrado após Portão POP limpo")
