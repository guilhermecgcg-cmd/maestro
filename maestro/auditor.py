"""Auditor: o anti-besteira. Verifica cada entrega de forma INDEPENDENTE, contra o
sistema real, do ponto de vista do usuário. Não confia em "retornou 200" nem no
relato de quem implementou. Contradição entre fontes = falha automática. Sem laudo
100% verde, não existe "entregue". (Nasceu do erro da Atena: 0 aulas em cima e 586
embaixo, declarado 100% funcional.)"""
from dataclasses import dataclass


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
