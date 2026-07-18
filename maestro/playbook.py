"""Playbook das falhas CONHECIDAS: mapeia Problema -> ação segura (via Acesso) e
devolve o que foi feito (pra Voz avisar). Ação insegura/ambígua NÃO é executada:
vira escalada (o Cérebro diagnostica ou o humano decide)."""
from dataclasses import dataclass

from maestro.sentinela import Problema


@dataclass(frozen=True)
class Acao:
    descricao: str
    executada: bool
    escalar: bool
    pedido: str = ""


def resolver(p: Problema, acesso) -> Acao:
    if p.tipo == "servico_caido":
        acesso.restart(p.alvo)
        return Acao(f"serviço {p.alvo} estava caído — reiniciei", True, False)
    # ambíguos/destrutivos -> escala (não age)
    if p.tipo == "servico_doente":
        return Acao("", False, True,
                    f"serviço {p.alvo} está Up mas doente (/health falhou) — diagnosticar")
    if p.tipo == "servico_restart_loop":
        return Acao("", False, True, f"serviço {p.alvo} em restart-loop — preciso diagnosticar")
    if p.tipo == "disco_alto":
        return Acao("", False, True, f"disco alto ({p.detalhe}) — limpeza precisa de OK humano")
    return Acao("", False, True, f"problema não-mapeado: {p.tipo}")
