"""Ciclo UNIVERSAL do Maestro: itera os projetos do registro. Por projeto monta o
snapshot (serviços do projeto + /health + recursos), roda os checks genéricos
(Sentinela) + os do adaptador (se houver), resolve (Playbook genérico; restart-loop/
doente -> Cérebro; problemas de adaptador -> adaptador) e reporta (Voz). Genérico e
testável com dublês."""
import asyncio
import time
from types import SimpleNamespace

from maestro import sentinela, playbook, observador, orquestrador, adaptador_pipeline
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


def ciclo(acesso, voz, projetos, *, llm, estado=None, db=None, voo=None,
          orquestrar_cursos=False, sintese_fn=None, plataformas_suportadas=None) -> list:
    todas = acesso.servicos()
    acoes = []
    if estado is None:
        estado = {}
    # `voo` = estado cross-ciclo da Camada 2 (passadas "em voo"). O CHAMADOR (run)
    # o cria UMA vez e o REINJETA a cada ciclo — sem isso a confirmação cross-ciclo
    # de um curso nunca acontece (contrato do orquestrador). Um `voo` local aqui
    # (quando o chamador não injeta) NÃO persiste entre ciclos: só serve p/ um ciclo
    # avulso não quebrar. `run` sempre injeta o mesmo dict.
    if voo is None:
        voo = {}

    # SONDA ÚNICA POR CICLO (dedup): /health e df/free são idempotentes mas custam
    # rede/IO na VPS. Sondá-los UMA vez e REUSAR mata a sondagem dupla que existia
    # (o observador da Camada 1 sondava, e o laço por-projeto sondava de novo os
    # MESMOS /health e df/free). `todas` (docker ps) já veio acima. saude_http vai
    # UMA vez sobre a UNIÃO dos alvos de todos os projetos — cada projeto fatia o
    # seu depois; recursos() é global -> uma leitura basta.
    alvos_saude = {}
    for proj in projetos:
        alvos_saude.update(getattr(proj, "saude", {}) or {})
    saude_all = acesso.saude_http(alvos_saude) if alvos_saude else {}
    recursos_all = acesso.recursos()

    # CAMADA 1 (os olhos): quando um store durável `db` é injetado, grava um
    # snapshot carimbado do estado REAL a cada ciclo (serviços up/health +
    # recursos). É a correção da CEGUEIRA (P1): o estado passa a ser uma série
    # temporal — o cérebro pergunta "ficou up nos últimos N min?" (flapping/
    # estado_estavel), não "está up neste instante?" (sonda). db=None -> desligado
    # (retrocompatível; nada muda no loop atual). Reusa a sonda única do ciclo
    # (servicos/saude/recursos) — não re-sonda; carimba tudo com seu próprio `agora`.
    if db is not None:
        try:
            observador.registrar(
                db, observador.coletar_estado(
                    acesso, alvos_saude, time.time(), servicos_raw=todas,
                    saude=saude_all, recursos_raw=recursos_all))
        except Exception:
            pass  # observar nunca pode derrubar o loop de saúde
    for proj in projetos:
        servs = {n: s for n, s in todas.items() if n in proj.servicos}
        saude_proj = {n: saude_all.get(n, False)
                      for n in (getattr(proj, "saude", {}) or {})}
        snap = {"servicos": servs, "saude": saude_proj,
                "recursos": recursos_all, "agora": time.time()}
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
            if orquestrar_cursos and alvo_app:
                # CAMADA 2 (o CÉREBRO) sobre os cursos: lê o estado OBSERVADO (progresso
                # REAL no Notion NESTE ciclo) + o ESPERADO (enumeração) e roda o feedback
                # loop cross-ciclo com o `voo` REINJETADO. É AQUI que uma passada fica "em
                # voo" e só confirma/escala em ciclos POSTERIORES. `estado.progresso`
                # reflete o Notion lido agora (nunca flag). DESLIGADO por default: quando
                # ligado, ELE (não o coordenar) é o dono do disparo dos cursos — ver a
                # pendência de design registrada no relatório (coordenar ainda detém
                # anti-dup por completude + checagem de sessão + auto-ingest).
                acoes.extend(_orquestrar_cursos(
                    proj, acesso, voz, executor, prog_notion_fn,
                    agora=snap["agora"], voo=voo))
            else:
                for curso_url in proj.cursos_desejados:
                    st = cap_estado.setdefault(curso_url, {})
                    # CAPACIDADE B (gatilho): plataforma NOVA (sem adaptador). Só checa
                    # quando `plataformas_suportadas` é fornecido (None => desligado,
                    # retrocompatível). Fail-closed CONSERVADOR: NÃO capturamos numa
                    # plataforma sem adaptador (o motor só sabe Hotmart) e NÃO construímos
                    # o adaptador sozinhos — recon/build/deploy na infra paga exigem
                    # decisão humana. Escalamos UMA vez (latch) e pulamos o curso.
                    if (plataformas_suportadas is not None and
                            not adaptador_pipeline.plataforma_suportada(
                                curso_url, plataformas_suportadas)):
                        acao = _escalar_plataforma_nova(proj, voz, curso_url, st)
                        if acao is not None:
                            acoes.append(acao)
                        continue
                    acao = captura.coordenar(proj, acesso, voz, executor=executor,
                                             curso_url=curso_url, estado=st, agora=snap["agora"],
                                             progresso_notion_fn=prog_notion_fn,
                                             sintese_fn=sintese_fn)
                    if acao is not None:
                        acoes.append(acao)
    return acoes


def _escalar_plataforma_nova(proj, voz, curso_url, st):
    """Gatilho da Capacidade B: escala UMA vez (latch por-curso) que a plataforma é
    NOVA (sem adaptador). NÃO dispara o pipeline de criação de adaptador
    (adaptador_pipeline.rodar_pipeline) — recon autenticado + build + deploy na infra
    paga são decisão do humano; o pipeline existe e está pronto, mas o gatilho apenas
    ESCALA e aguarda aprovação (conservador, fail-closed)."""
    if st.get("plataforma_nova_avisada"):
        return None
    plat = adaptador_pipeline.plataforma_de_url(curso_url)
    pedido = (f"[{proj.nome}] {curso_url} está numa PLATAFORMA NOVA "
              f"('{plat or 'desconhecida'}', sem adaptador). NÃO capturo sem adaptador; "
              f"criar o adaptador (recon->Portão POP->build->deploy) precisa da SUA "
              f"aprovação — pipeline pronto, gatilho aguardando decisão.")
    voz.escalar(sentinela.Problema("plataforma_nova", curso_url, pedido, "aviso"), pedido)
    st["plataforma_nova_avisada"] = True
    return playbook.Acao("", False, True, pedido)


# formas do estado OBSERVADO que o orquestrador (Camada 2) consome. Espelham
# observador.ServicoObservado/ProgressoCurso no que o orquestrador de fato lê.
def _prog_obs(curso, done, total):
    return SimpleNamespace(curso=curso, done=int(done), total=int(total))


def _orquestrar_cursos(proj, acesso, voz, executor, prog_notion_fn, *, agora, voo):
    """Monta o snapshot OBSERVADO por-curso (done = contagem REAL no Notion; total =
    enumeração esperada) e roda `orquestrador.orquestrar` com o `voo` reinjetado.
    Curso cujo Notion/enumeração não dá para ler NESTE ciclo é PULADO (não vira
    divergência às cegas). Devolve os Resultados (o `run` os ignora; testes os leem)."""
    from maestro.adaptadores import captura
    progresso, esperado_cursos = [], {}
    for curso_url in proj.cursos_desejados:
        try:
            no_notion = prog_notion_fn(curso_url).no_notion
        except Exception:
            continue                      # não lê o Notion agora -> não diagnostica às cegas
        total = captura._total_esperado(proj, acesso, curso_url, None)
        progresso.append(_prog_obs(curso_url, no_notion, total))
        esperado_cursos[curso_url] = total
    est_obs = SimpleNamespace(servicos=(), progresso=tuple(progresso))
    acoes_ath = orquestrador.AcoesAthena(acesso, proj, executor=executor)
    # verificar só é consultado p/ serviços (efeito imediato); aqui só há cursos
    # (assíncronos, confirmados via `voo`), então nunca é chamado.
    return orquestrador.orquestrar(est_obs, {"cursos": esperado_cursos}, acoes_ath,
                                   lambda div: False, voz, agora=agora, voo=voo)


async def run(acesso, voz, projetos, *, llm, sleep=asyncio.sleep, intervalo_s=120.0,
              max_iters=None, db=None, orquestrar_cursos=False, sintese_fn=None,
              plataformas_suportadas=None):
    i = 0
    estado = {}
    # `voo` (Camada 2): criado UMA vez e REINJETADO a cada ciclo. É o store cross-ciclo
    # onde as passadas ficam "em voo" — sem reinjetar o MESMO dict, a confirmação de um
    # curso (que só chega em ciclos posteriores) nunca aconteceria.
    voo = {}
    while max_iters is None or i < max_iters:
        i += 1
        try:
            ciclo(acesso, voz, projetos, llm=llm, estado=estado, db=db, voo=voo,
                  orquestrar_cursos=orquestrar_cursos, sintese_fn=sintese_fn,
                  plataformas_suportadas=plataformas_suportadas)
        except Exception:
            pass
        await sleep(intervalo_s)
    return i


async def servir(acesso, voz, projetos, *, llm, athena=None, db=None,
                 orquestrar_cursos=False, sintese_fn=None, plataformas_suportadas=None,
                 intervalo_s=120.0, max_iters=None, sleep_saude=asyncio.sleep,
                 athena_intervalo=25, athena_max_iters=None,
                 offset_load=None, offset_save=None):
    """Entrelaça a Camada 1+2 (loop de saúde/orquestração) com a Camada 3 (listener
    de comandos da Athena no Telegram) como tarefas CONCORRENTES. O listener é
    SÍNCRONO e bloqueante (long-poll), então roda numa thread (`asyncio.to_thread`)
    em paralelo ao loop de saúde — um não trava o outro.

    A Athena NUNCA comanda a infra sem whitelist: `athena` já vem com `autorizados`
    plumbados (fail-closed) e o offset do Telegram PERSISTIDO via offset_load/save
    (sem isso, um restart reprocessaria comandos antigos). Sem `athena`, roda só o
    loop de saúde (retrocompatível)."""
    tarefas = [run(acesso, voz, projetos, llm=llm, sleep=sleep_saude,
                   intervalo_s=intervalo_s, max_iters=max_iters, db=db,
                   orquestrar_cursos=orquestrar_cursos, sintese_fn=sintese_fn,
                   plataformas_suportadas=plataformas_suportadas)]
    if athena is not None:
        kw = {"intervalo": athena_intervalo, "max_iters": athena_max_iters}
        if offset_load is not None:
            kw["offset_load"] = offset_load
        if offset_save is not None:
            kw["offset_save"] = offset_save
        tarefas.append(asyncio.to_thread(athena.rodar, **kw))
    await asyncio.gather(*tarefas)
