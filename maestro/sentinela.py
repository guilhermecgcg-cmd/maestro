"""Detecção determinística e barata das falhas CONHECIDAS. Função pura sobre um
snapshot (o Acesso monta o snapshot; a Sentinela não faz I/O) — trivial de testar
e impossível de mentir."""
from dataclasses import dataclass

DISCO_ALERTA_PCT = 90.0


@dataclass(frozen=True)
class Problema:
    tipo: str
    alvo: str
    detalhe: str
    severidade: str  # "critico" | "aviso"


def checar(snapshot: dict) -> list:
    ps = []
    for nome, s in snapshot.get("servicos", {}).items():
        if s.restarting:
            ps.append(Problema("servico_restart_loop", nome, "container em restart-loop", "critico"))
        elif not s.up:
            ps.append(Problema("servico_caido", nome, "container não está Up", "critico"))
    saude = snapshot.get("saude", {})
    for nome, ok in saude.items():
        serv = snapshot.get("servicos", {}).get(nome)
        if serv is not None and serv.up and not ok:
            ps.append(Problema("servico_doente", nome,
                               "container Up mas /health falhou", "critico"))
    if snapshot.get("recursos", {}).get("disco_pct", 0) >= DISCO_ALERTA_PCT:
        ps.append(Problema("disco_alto", "vps",
                           f"disco em {snapshot['recursos']['disco_pct']:.0f}%", "critico"))
    return ps
