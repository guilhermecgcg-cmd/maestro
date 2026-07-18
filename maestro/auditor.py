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


def contradicoes(fontes: dict) -> list:
    out = []
    ta, pa = fontes.get("tela_aulas"), fontes.get("postgres_aulas")
    if ta is not None and pa is not None and ta != pa:
        out.append(f"nº de aulas: tela={ta} mas postgres={pa} (contradição)")
    return out
