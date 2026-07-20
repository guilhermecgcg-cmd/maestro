"""Ciclo UNIVERSAL do Maestro: itera os projetos do registro. Por projeto monta o
snapshot (serviços do projeto + /health + recursos), roda os checks genéricos
(Sentinela) + os do adaptador (se houver), resolve (Playbook genérico; restart-loop/
doente -> Cérebro; problemas de adaptador -> adaptador) e reporta (Voz). Genérico e
testável com dublês."""
import asyncio
import time

from maestro import sentinela, playbook, observador
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


def ciclo(acesso, voz, projetos, *, llm, estado=None, db=None) -> list:
    todas = acesso.servicos()
    acoes = []
    if estado is None:
        estado = {}

    # CAMADA 1 (os olhos): quando um store durável `db` é injetado, grava um
    # snapshot carimbado do estado REAL a cada ciclo (serviços up/health +
    # recursos). É a correção da CEGUEIRA (P1): o estado passa a ser uma série
    # temporal — o cérebro pergunta "ficou up nos últimos N min?" (flapping/
    # estado_estavel), não "está up neste instante?" (sonda). db=None -> desligado
    # (retrocompatível; nada muda no loop atual). fila_fn/progresso_fn ficam como
    # pontos de injeção (a contagem-verdade do Notion é costura futura).
    if db is not None:
        alvos = {}
        for proj in projetos:
            alvos.update(getattr(proj, "saude", {}) or {})
        try:
            observador.registrar(
                db, observador.coletar_estado(acesso, alvos, time.time()))
        except Exception:
            pass  # observar nunca pode derrubar o loop de saúde
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
            # GATE I-1 (completude por Notion, não por flag): liga o seam real da
            # contagem-verdade. Quando o tracker disser 'done', o coordenar CONFIRMA
            # contra o Notion antes de declarar concluído — falso-pronto é rejeitado.
            # Sem app_container não há como confirmar -> seam None (o coordenar cai no
            # comportamento antigo em vez de escalar todo ciclo por um registro incompleto).
            alvo_app = getattr(proj, "app_container", "")
            prog_notion_fn = (
                (lambda u: captura.progresso_curso_no_notion(acesso, alvo_app, u))
                if alvo_app else None)
            for curso_url in proj.cursos_desejados:
                st = cap_estado.setdefault(curso_url, {})
                acao = captura.coordenar(proj, acesso, voz, executor=executor,
                                         curso_url=curso_url, estado=st, agora=snap["agora"],
                                         progresso_notion_fn=prog_notion_fn)
                if acao is not None:
                    acoes.append(acao)
    return acoes


async def run(acesso, voz, projetos, *, llm, sleep=asyncio.sleep, intervalo_s=120.0,
              max_iters=None, db=None):
    i = 0
    estado = {}
    while max_iters is None or i < max_iters:
        i += 1
        try:
            ciclo(acesso, voz, projetos, llm=llm, estado=estado, db=db)
        except Exception:
            pass
        await sleep(intervalo_s)
    return i
