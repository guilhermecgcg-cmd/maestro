"""Ciclo UNIVERSAL do Maestro: itera os projetos do registro. Por projeto monta o
snapshot (serviços do projeto + /health + recursos), roda os checks genéricos
(Sentinela) + os do adaptador (se houver), resolve (Playbook genérico; restart-loop/
doente -> Cérebro; problemas de adaptador -> adaptador) e reporta (Voz). Genérico e
testável com dublês."""
import asyncio
import time

from maestro import sentinela, playbook
from maestro.cerebro import diagnosticar


def _resolver(p, acesso, proj, llm):
    # problemas específicos do projeto -> adaptador
    if p.tipo in ("job_travado", "job_falhou", "claim_orfao", "conhecimento_db_inacessivel"):
        if proj.adaptador == "conhecimento":
            from maestro.adaptadores import conhecimento
            return conhecimento.resolver(p, acesso, proj)
        return playbook.Acao("", False, True, f"[{proj.nome}] {p.tipo} (sem adaptador)")
    acao = playbook.resolver(p, acesso)
    if acao.escalar and p.tipo in ("servico_restart_loop", "servico_doente"):
        d = diagnosticar(p, acesso.logs(p.alvo), llm)
        if d.acao == "restart" and not d.escalar:
            acesso.restart(p.alvo)
            acao = playbook.Acao(f"[{proj.nome}] {p.alvo}: {d.diagnostico} — reiniciei", True, False)
        elif d.acao == "redeploy" and not d.escalar:
            acesso.redeploy(p.alvo, proj.projeto_easypanel)
            acao = playbook.Acao(f"[{proj.nome}] {p.alvo}: {d.diagnostico} — redeployei", True, False)
        else:
            acao = playbook.Acao("", False, True, f"[{proj.nome}] {p.alvo}: {d.diagnostico}")
    return acao


def ciclo(acesso, voz, projetos, *, llm) -> list:
    todas = acesso.servicos()
    acoes = []
    for proj in projetos:
        servs = {n: s for n, s in todas.items() if n in proj.servicos}
        snap = {"servicos": servs, "saude": acesso.saude_http(proj.saude),
                "recursos": acesso.recursos(), "agora": time.time()}
        problemas = list(sentinela.checar(snap))
        if proj.adaptador == "conhecimento":
            from maestro.adaptadores import conhecimento
            problemas += conhecimento.checar(proj, acesso)
        for p in problemas:
            if not proj.gerenciar:
                # projeto monitorado (não opt-in): só avisa, NUNCA age sozinho.
                voz.escalar(p, f"[{proj.nome}] {p.tipo}: {p.detalhe} "
                               f"(monitorado; diga 'gerencia {proj.nome}' pra eu agir)")
                acoes.append(playbook.Acao("", False, True))
                continue
            acao = _resolver(p, acesso, proj, llm)
            if acao.executada:
                voz.avisar_acao(acao)
            elif acao.escalar:
                voz.escalar(p, acao.pedido)
            acoes.append(acao)
    return acoes


async def run(acesso, voz, projetos, *, llm, sleep=asyncio.sleep, intervalo_s=120.0, max_iters=None):
    i = 0
    while max_iters is None or i < max_iters:
        i += 1
        try:
            ciclo(acesso, voz, projetos, llm=llm)
        except Exception:
            pass
        await sleep(intervalo_s)
    return i
