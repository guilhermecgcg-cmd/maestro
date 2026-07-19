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
    if p.tipo in ("job_travado", "job_falhou", "claim_orfao", "conhecimento_db_inacessivel", "captura_vazia"):
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


def ciclo(acesso, voz, projetos, *, llm, estado=None) -> list:
    todas = acesso.servicos()
    acoes = []
    if estado is None:
        estado = {}
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

        # ROTINA periodica (nao e "problema"): PONTE AUTO-INGEST Notion->pgvector.
        # So projetos gerenciados e com o adaptador conhecimento; o proprio
        # reconciliar decide se a janela venceu (estado por projeto entre ciclos).
        if proj.gerenciar and proj.adaptador == "conhecimento":
            from maestro.adaptadores import conhecimento
            acao, estado[proj.nome] = conhecimento.reconciliar(
                proj, acesso, agora=snap["agora"], ultimo=estado.get(proj.nome, 0.0))
            if acao is not None:
                if acao.executada:
                    voz.avisar_acao(acao)
                elif acao.escalar:
                    voz.escalar(sentinela.Problema("reconcile", proj.nome, acao.pedido, "aviso"),
                                acao.pedido)
                acoes.append(acao)

        # ROTINA periodica: PROTOCOLO DE CAPTURA por curso desejado. Fonte = a lista
        # `cursos_desejados` (course_urls) do PROPRIO projeto do conhecimento — VAZIA
        # por default, entao nada e auto-disparado ate ser populada. NAO cria entrada
        # nova no registro (mesmos containers -> evita monitoramento duplicado). Estado
        # por-curso persiste entre ciclos num dict aninhado sob a chave do projeto. O
        # coordenar e dono do seu reporte (dirige a voz sozinho); o loop so registra.
        if proj.gerenciar and getattr(proj, "cursos_desejados", ()):
            from maestro.adaptadores import captura
            cap_estado = estado.setdefault(f"{proj.nome}::captura", {})
            executor = captura.FilaExecutor(acesso, proj)
            for curso_url in proj.cursos_desejados:
                st = cap_estado.setdefault(curso_url, {})
                acao = captura.coordenar(proj, acesso, voz, executor=executor,
                                         curso_url=curso_url, estado=st, agora=snap["agora"])
                if acao is not None:
                    acoes.append(acao)
    return acoes


async def run(acesso, voz, projetos, *, llm, sleep=asyncio.sleep, intervalo_s=120.0, max_iters=None):
    i = 0
    estado = {}
    while max_iters is None or i < max_iters:
        i += 1
        try:
            ciclo(acesso, voz, projetos, llm=llm, estado=estado)
        except Exception:
            pass
        await sleep(intervalo_s)
    return i
