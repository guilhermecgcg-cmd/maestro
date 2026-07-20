"""Auditor: o anti-besteira. Verifica cada entrega de forma INDEPENDENTE, contra o
sistema real, do ponto de vista do usuário. Não confia em "retornou 200" nem no
relato de quem implementou. Contradição entre fontes = falha automática. Sem laudo
100% verde, não existe "entregue". (Nasceu do erro da Atena: 0 aulas em cima e 586
embaixo, declarado 100% funcional.)"""
from dataclasses import dataclass

from maestro.sentinela import Problema


@dataclass(frozen=True)
class Criterio:
    nome: str
    checar: object   # callable(ctx) -> (ok: bool, evidencia: str)


@dataclass(frozen=True)
class ResultadoCriterio:
    nome: str
    ok: bool
    evidencia: str


@dataclass(frozen=True)
class Laudo:
    resultados: list
    aprovado: bool


def auditar(criterios: list, ctx) -> Laudo:
    res = []
    for c in criterios:
        try:
            ok, ev = c.checar(ctx)
        except Exception as e:
            ok, ev = False, f"erro ao verificar: {e}"
        res.append(ResultadoCriterio(c.nome, bool(ok), str(ev)))
    # fail-CLOSED: sem critério não há verificação -> NÃO aprova (é o modo de falha
    # da Atena que o Auditor existe pra impedir).
    return Laudo(res, bool(res) and all(r.ok for r in res))


def criterio_igual(nome: str, esperado, obtido, *, rotulo="valor") -> Criterio:
    """Critério I-2: o estado REAL tem de bater com a decisão explícita do usuário.
    Ex.: esperado = projeto Easypanel que o usuário decidiu, obtido = projeto onde
    o serviço de fato roda. Divergência reprova o laudo — bloqueia o "entregue".
    (Nasceu de 18/07: Maestro deployado dentro do conhecimento quando o usuário
    escolheu projeto próprio.)"""
    def _checar(_ctx):
        ok = esperado == obtido
        ev = f"{rotulo}: decidido={esperado!r}, real={obtido!r}"
        if not ok:
            ev += "  <- CONTRADIÇÃO com a decisão do usuário (viola I-2)"
        return ok, ev
    return Criterio(nome, _checar)


def criterio_completude_notion(course_url, no_notion, total_esperado) -> Criterio:
    """Critério I-1: um curso só está COMPLETO quando a FONTE DE VERDADE (Notion) tem
    no_notion >= total_esperado (com total>0). É o anti-falso-pronto: o tracker/flag
    pode dizer 'done', mas só o Notion PROVA. Reprova com o gap honesto (X/Y, Z
    faltando). (Nasceu de 'pronto' declarado com 466 aulas pendentes.)"""
    def _checar(_ctx):
        n = int(no_notion)
        alvo = int(total_esperado)
        ok = alvo > 0 and n >= alvo
        ev = f"completude: Notion tem {n}/{alvo}"
        if not ok:
            falta = max(alvo - n, 0)
            ev += (f" — reportado pronto mas {falta} faltando"
                   "  <- FALSO-PRONTO REJEITADO (viola I-1)")
        return ok, ev
    return Criterio("completude por Notion (I-1)", _checar)


def auditar_conclusao(job, progresso_notion, total_esperado, *, voz=None) -> Laudo:
    """GATE I-1: um job/curso reportado 'pronto' só é CONFIRMADO completo se o Notion
    tiver `progresso_notion.no_notion >= total_esperado`. Senão, o Laudo é REJEITADO
    com o gap honesto e (se `voz`) ESCALA — nunca confia num flag 'pronto'. `total_
    esperado` vem da ENUMERAÇÃO (injetado); a Athena não enumera aqui."""
    c = criterio_completude_notion(progresso_notion.course_url,
                                   progresso_notion.no_notion, total_esperado)
    laudo = auditar([c], ctx={})
    if not laudo.aprovado and voz is not None:
        n = int(progresso_notion.no_notion)
        alvo = int(total_esperado)
        falta = max(alvo - n, 0)
        pedido = (f"[{job}] reportado PRONTO mas o Notion tem {n}/{alvo} — {falta} "
                  f"faltando (falso-pronto REJEITADO, I-1); NÃO declaro concluído")
        voz.escalar(Problema("falso_pronto", str(job), pedido, "critico"), pedido)
    return laudo


def contradicoes(fontes: dict) -> list:
    out = []
    ta, pa = fontes.get("tela_aulas"), fontes.get("postgres_aulas")
    if ta is not None and pa is not None and ta != pa:
        out.append(f"nº de aulas: tela={ta} mas postgres={pa} (contradição)")
    # I-2: estado de arquitetura real vs. decidido pelo usuário
    da, ra = fontes.get("arquitetura_decidida"), fontes.get("arquitetura_real")
    if da is not None and ra is not None and da != ra:
        out.append(f"arquitetura: decidido={da} mas real={ra} (contradição I-2)")
    return out
