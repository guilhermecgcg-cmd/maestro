"""ENTRYPOINT DOMÉSTICO da Athena — o loop que roda NO MAC (`python -m maestro.athena_local`).

Decisão de arquitetura (INVIOLÁVEL): a Athena roda DOMÉSTICA no Mac, NÃO na VPS. O
executor de captura, aqui, é o `captura.LocalExecutor` (chama o MOTOR DIRETO como
subprocesso), NÃO o `FilaExecutor` (que enfileira pro worker da VPS). Este módulo é o
`main.py` do modelo doméstico: monta o executor local + a contagem-verdade do Notion
LOCAL e roda o mesmo LOOP-DONO já testado.

REUSO (não reimplementação): a COORDENAÇÃO é a MESMA `orquestrador.orquestrar_captura`
do modelo VPS — completude-por-Notion, vigília de STALL, uma-passada-por-ciclo,
fail-closed quando uma passada estoura. O que muda é só a `passada_fn` (o EXECUTOR):
de `coordenar(executor=FilaExecutor)` para uma passada LOCAL enxuta sobre o
`LocalExecutor`.

=====================================================================================
INTEGRAÇÃO DAS 6 PARTES (never-stop + controle), FIADAS AQUI (ponto único de costura):
  P5 CONTROLE   — `controle.filtrar_cursos(cursos, path)` a cada ciclo: plataforma/conta
                  pausada em `controle.yaml` NÃO é RE-disparada (nunca mata processo vivo).
  P4 DISJUNTOR  — `disjuntor.pode_tentar/registrar_falha/registrar_sucesso(st, agora)`:
                  RECOZIMENTO (backoff re-armável 10min→1h→6h→24h) no gate de re-tentativa,
                  no lugar do teto permanente. Causa IRREDUTÍVEL (reseed/token) abre de vez.
  P3 AUTÓPSIA   — na MORTE de um curso, `vigia.autopsia(lock_dir, obitos)` colhe a evidência
                  (exit_code/stderr/flap) e `causa.classificar(obito)` decide a causa-raiz.
  P2 BATIMENTO  — `batimento.talvez_bater(voz, resumo, agora, ultimo)` (pulso de vida) +
     /ALERTAS     `alertas` typados na morte/flap/sessão. Fail-safe: mudo LOGA, não POSTA.
  P1 COMPOSIÇÃO — a costura em si: mapeia a decisão da causa em ação do disjuntor/alerta.

A costura vive TODA neste arquivo (o loop). Cada colaborador entra INJETADO; o DEFAULT é
um NULL-OBJECT que preserva EXATAMENTE o comportamento pré-integração — de modo que os
testes-invariante do loop rodam sem as 6 partes e o daemon degrada com graça. As
assinaturas dos null-objects BATEM com as dos módulos REAIS (não com contratos idealizados).
=====================================================================================

PENDENTE / o que ficou por LIGAR (honesto):
  - A lista de cursos vem de um YAML (ATHENA_LOCAL_CURSOS); a ponte para o cadastro do
    Painel/Notion não está ligada.
  - `total_esperado` (denominador da completude) vem do YAML; sem ele (0) o owner nunca
    conclui (fail-closed). Não há oráculo local de quantas aulas o curso tem.
  - AUTO-INGEST/SINTETIZADOR pós-captura (esteira downstream) não roda aqui.
  - STDERR na autópsia: o `_spawn_popen` default manda stderr→DEVNULL, então a causa-raiz
    doméstica classifica pelo EXIT_CODE (forte). Assinaturas de sessão/token no stderr só
    ficam disponíveis com um spawn que tee'a o stderr por conta — costura pronta para
    recebê-lo (o vigia lê `stderr_tail`/`stderr_path`), mas o tee ainda não está ligado.
"""
import asyncio
import os
import time

from maestro import adaptador_pipeline, orquestrador
from maestro.adaptadores import captura
from maestro.playbook import Acao
from maestro.sentinela import Problema
from maestro.vigia import _FLAP_MIN as _FLAP_MIN_PADRAO

# Fase por-curso (reusa as do adaptador de captura — mesmo vocabulário de máquina de
# estados; só as fases NOVO/CAPTURANDO/CONCLUIDO importam no doméstico).
FASE_NOVO = captura.FASE_NOVO
FASE_CAPTURANDO = captura.FASE_CAPTURANDO
FASE_CONCLUIDO = captura.FASE_CONCLUIDO

# Ações da causa-raiz (maestro.causa.ACOES) que exigem HUMANO e NÃO devem ser marteladas:
# a sessão/credencial só o dono conserta (reseed HEADED / troca de token). Marcam o curso
# como IRREDUTÍVEL -> o disjuntor abre de vez (não re-tenta) até o humano agir.
_CAUSAS_IRREDUTIVEIS = ("escalar_reseed", "escalar_token")


# ---------------------------------------------------------------------------
# NULL-OBJECTS (defaults) — assinaturas IDÊNTICAS às dos módulos reais (P3/P4/P5/P2),
# de modo que injetar o módulo real ou o default é transparente para o loop.
# ---------------------------------------------------------------------------
class _ControleNulo:
    """DEFAULT P5: nada pausado (a lista inteira flui). Assinatura == controle.filtrar_cursos."""
    def filtrar_cursos(self, cursos, path=None):
        return list(cursos)


class _DisjuntorTeto:
    """DEFAULT P4: o TETO FIXO de tentativas pré-integração (SEM recozimento). Assinaturas
    == disjuntor.{pode_tentar,registrar_falha,registrar_sucesso}(st, agora). `pode_tentar`
    abre enquanto tentativas < max e fecha no teto; `registrar_falha/sucesso` são no-op
    (o contador `tentativas` sobe no disparo). O disjuntor REAL (backoff re-armável)
    substitui isto quando injetado — sem tocar na fiação."""
    def __init__(self, max_tentativas):
        self._max = max_tentativas

    def pode_tentar(self, st, agora):
        if st.get("irredutivel"):
            return False
        return st.get("tentativas", 0) < self._max

    def registrar_falha(self, st, agora):
        return None

    def registrar_sucesso(self, st):
        return None


class _VigiaNulo:
    """DEFAULT P3: sem autópsia (nenhum óbito a colher). Assinatura == vigia.autopsia."""
    def autopsia(self, lock_dir, stderr_por_conta, **kw):
        return []


class _CausaNula:
    """DEFAULT P3: nunca chamada (o vigia nulo não devolve óbitos)."""
    def classificar(self, obito, llm=None):
        return None


class _BatimentoNulo:
    """DEFAULT P2: relógio intacto (nenhum pulso). Assinatura == batimento.talvez_bater."""
    def talvez_bater(self, voz, resumo_fn, agora, ultimo, intervalo=1800):
        return ultimo


class _AlertasNulo:
    """DEFAULT P2: alertas typados viram no-op (o loop segue sem canal de supervisão)."""
    def captura_morreu(self, plataforma, motivo):
        return None

    def sessao_expirada(self, plataforma):
        return None

    def curso_concluido(self, plataforma, curso, n):
        return None


def _safe_ativo(executor, curso_url) -> bool:
    try:
        return bool(executor.curso_ativo(curso_url))
    except Exception:
        return False


def _plataforma_de(curso_url, meta_por_curso) -> str:
    """Rótulo de plataforma para os alertas typados. Prefere a `plataforma` do CursoLocal;
    cai na inferência por URL; por fim, a própria URL (nunca vazio)."""
    meta = (meta_por_curso or {}).get(curso_url)
    if meta is not None and getattr(meta, "plataforma", ""):
        return meta.plataforma
    return adaptador_pipeline.plataforma_de_url(curso_url) or curso_url


# ---------------------------------------------------------------------------
# AUTÓPSIA + CAUSA-RAIZ (P3) fiadas na MORTE de um curso -> decisão do disjuntor (P4)
# ---------------------------------------------------------------------------
def _aplicar_decisao(curso, st, obito, decisao, *, disjuntor, alertas, agora,
                     meta_por_curso, flap_min):
    """Traduz a `Decisao` da causa-raiz em ação de never-stop sobre o `st` do curso.

      transitório (relancar/aguardar_backoff/None) -> `registrar_falha` avança o backoff
        do disjuntor; o curso volta a NOVO e re-tenta SOB o gate (recozimento, não martelo).
      IRREDUTÍVEL (escalar_reseed/escalar_token) -> marca `st['irredutivel']`: o disjuntor
        abre de vez (não re-tenta) e ALERTA o humano (a sessão/credencial só ele conserta).
      desconhecida (escalar_humano) -> back off + ALERTA (fail-closed: chama o dono).
      FLAP (>= flap_min mortes na janela) -> ALERTA de captura morrendo em loop, seja qual
        for a causa.
    """
    acao = getattr(decisao, "acao", None)
    plat = _plataforma_de(curso, meta_por_curso)
    st["ultima_causa"] = acao
    st["_morte_ciclo"] = agora                       # a passada NÃO re-trata esta morte

    flaps = int(getattr(obito, "flaps_na_janela", 0) or 0)
    if flaps >= flap_min:
        alertas.captura_morreu(
            plat, f"FLAP: {flaps} mortes na janela (curso {curso}, causa={acao})")

    if acao in _CAUSAS_IRREDUTIVEIS:
        st["irredutivel"] = True
        st["esgotado_avisado"] = True                # o alerta typado abaixo já cobre
        if acao == "escalar_reseed":
            alertas.sessao_expirada(plat)
        else:  # escalar_token
            alertas.captura_morreu(
                plat, f"credencial de API inválida — troque o token (curso {curso})")
        st["fase"] = FASE_NOVO
        return

    # transitório OU desconhecida: back off (recozimento) e re-tenta sob o gate.
    try:
        disjuntor.registrar_falha(st, agora)
    except Exception:
        pass
    if acao == "escalar_humano":
        alertas.captura_morreu(
            plat, f"causa desconhecida (fail-closed): {getattr(decisao, 'motivo', '')}")
    st["fase"] = FASE_NOVO


def _autopsiar_ciclo(executor, estado, *, vigia, causa, disjuntor, alertas, lock_dir,
                     autopsia_dir, agora, meta_por_curso, flap_min, llm):
    """Passe de AUTÓPSIA do ciclo: drena os óbitos do executor, roda o vigia (que também
    varre `lock_dir` por mortes de encarnações anteriores), classifica cada óbito pela
    causa-raiz e aplica a decisão ao `st` do curso. Best-effort: um erro aqui NÃO derruba
    o ciclo (a passada ainda tem o fallback de morte)."""
    stderr_por_conta = {}
    drenar = getattr(executor, "drenar_obitos", None)
    if callable(drenar):
        try:
            stderr_por_conta = drenar() or {}
        except Exception:
            stderr_por_conta = {}
    try:
        obitos = vigia.autopsia(lock_dir, stderr_por_conta, agora=agora,
                                autopsia_dir=autopsia_dir, flap_min=flap_min)
    except Exception:
        obitos = []
    for obito in obitos:
        curso = getattr(obito, "curso", "") or ""
        if not curso:
            continue
        st = estado.setdefault(curso, {})
        try:
            decisao = causa.classificar(obito, llm=llm)
        except Exception:
            decisao = None
        _aplicar_decisao(curso, st, obito, decisao, disjuntor=disjuntor,
                         alertas=alertas, agora=agora, meta_por_curso=meta_por_curso,
                         flap_min=flap_min)


def _escalar_plataforma_nova(projeto_nome, voz, curso_url, st):
    """Gate da Capacidade B no doméstico: escala UMA vez (latch por-curso) que a
    plataforma é NOVA (sem adaptador) e PULA o curso. Não dispara criação de adaptador
    (decisão humana)."""
    if st.get("plataforma_nova_avisada"):
        return None
    plat = adaptador_pipeline.plataforma_de_url(curso_url)
    pedido = (f"[{projeto_nome}] {curso_url} está numa PLATAFORMA NOVA "
              f"('{plat or 'desconhecida'}', sem adaptador). NÃO capturo sem adaptador; "
              f"criar o adaptador precisa da SUA aprovação.")
    voz.escalar(Problema("plataforma_nova", curso_url, pedido, "aviso"), pedido)
    st["plataforma_nova_avisada"] = True
    return Acao("", False, True, pedido)


def _passada_local_fn(executor, progresso_fn, voz, estado, *, projeto_nome, disjuntor,
                      agora):
    """Fábrica da `passada_fn` LOCAL que o owner (`orquestrar_captura`) invoca por curso.

    A máquina de estados por-curso (persiste em `estado[curso]` entre ciclos):
      1. já CONCLUIDO -> None (idempotente).
      2. lê a contagem-verdade do Notion. Ilegível -> escala honesto, NÃO dispara às cegas.
      3. AVANÇO detectado (no_notion subiu) -> `disjuntor.registrar_sucesso` re-arma o
         recozimento (backoff volta ao degrau zero).
      4. COMPLETO no Notion (no_notion >= total, total>0) -> anti-dup por COMPLETUDE.
      5. processo AINDA ativo -> quieto (None): captura em andamento (anti-ban/disjuntor).
      6. FALLBACK de morte: estava CAPTURANDO, não está mais ativo, e a autópsia do ciclo
         NÃO tratou -> conta uma falha (backoff) e volta a NOVO (never-stop).
      7. `disjuntor.pode_tentar` FECHADO (teto/backoff/irredutível) -> escala UMA vez (latch).
      8. senão -> DISPARA. `ContaOcupada` (anti-ban) => aguarda a vez (None). Falha/silêncio
         do disparo -> escala honesto.
    """
    def passada(curso):
        st = estado.setdefault(curso, {})
        if st.get("fase") == FASE_CONCLUIDO:
            return None
        try:
            no_notion, total = progresso_fn(curso)
        except Exception as e:
            pedido = (f"[{projeto_nome}] NÃO consegui ler o Notion p/ {curso} "
                      f"(anti-dup/completude): {str(e)[:140]} — NÃO disparo às cegas")
            voz.escalar(Problema("progresso_notion_inacessivel", curso, pedido, "aviso"),
                        pedido)
            return Acao("", False, True, pedido)
        # AVANÇO -> re-arma o recozimento do disjuntor (o backoff reduz com sucesso).
        if no_notion is not None:
            prev = st.get("ultimo_no_notion")
            if prev is not None and int(no_notion) > int(prev):
                try:
                    disjuntor.registrar_sucesso(st)
                except Exception:
                    pass
            st["ultimo_no_notion"] = int(no_notion)
        if no_notion is not None and int(total) > 0 and int(no_notion) >= int(total):
            st["fase"] = FASE_CONCLUIDO
            acao = Acao(f"[{projeto_nome}] {curso} COMPLETO no Notion "
                        f"({no_notion}/{total}) — não disparo (anti-dup por completude)",
                        True, False)
            voz.avisar_acao(acao)
            return acao
        if executor.curso_ativo(curso):
            return None                                    # capturando: quieto
        # FALLBACK de morte (default/no-autópsia): estava CAPTURANDO e caiu; se a autópsia
        # do ciclo NÃO tratou esta morte, conta a falha (backoff) e reabilita a tentativa.
        if st.get("fase") == FASE_CAPTURANDO and st.get("_morte_ciclo") != agora:
            try:
                disjuntor.registrar_falha(st, agora)
            except Exception:
                pass
            st["fase"] = FASE_NOVO
        try:
            pode = disjuntor.pode_tentar(st, agora)
        except Exception:
            pode = True
        if not pode:
            pedido = (f"[{projeto_nome}] {curso} NÃO concluiu e o processo não está mais "
                      f"ativo — DISJUNTOR ABERTO (teto/backoff/irredutível): paro de "
                      f"disparar e escalo (não martelo). Re-arma quando o backoff expira / "
                      f"há avanço; irredutível (reseed/token) só o humano destrava.")
            if not st.get("esgotado_avisado"):
                voz.escalar(Problema("captura_local_esgotada", curso, pedido, "critico"),
                            pedido)
                st["esgotado_avisado"] = True
            return Acao("", False, True, pedido)
        st["esgotado_avisado"] = False                     # disjuntor reabriu: re-arma o latch
        try:
            conf = executor.disparar(curso)
        except captura.ContaOcupada:
            return None                                    # anti-ban: aguarda a vez (não é falha)
        except Exception as e:
            pedido = (f"[{projeto_nome}] FALHEI ao disparar a captura LOCAL de {curso}: "
                      f"{str(e)[:160]}")
            voz.escalar(Problema("captura_local_disparo_falhou", curso, pedido, "critico"),
                        pedido)
            return Acao("", False, True, pedido)
        if not conf:
            pedido = (f"[{projeto_nome}] disparo LOCAL de {curso} SEM confirmação — "
                      f"não assumo sucesso")
            voz.escalar(Problema("captura_local_sem_confirmacao", curso, pedido, "critico"),
                        pedido)
            return Acao("", False, True, pedido)
        st["tentativas"] = st.get("tentativas", 0) + 1
        st["fase"] = FASE_CAPTURANDO
        acao = Acao(f"[{projeto_nome}] captura LOCAL de {curso} iniciada "
                    f"(tentativa {st['tentativas']}): {conf}", True, False)
        voz.avisar_acao(acao)
        return acao
    return passada


def ciclo_local(cursos, executor, progresso_fn, voz, voo, estado, *, agora=None,
                plataformas_suportadas=None, max_tentativas=3, projeto_nome="athena-local",
                controle=None, controle_path=None, disjuntor=None, vigia=None, causa=None,
                alertas=None, lock_dir=None, autopsia_dir=None, meta_por_curso=None,
                flap_min=None, llm=None):
    """UM ciclo doméstico. Ordem: (1) CONTROLE filtra plataformas/contas PAUSADAS (P5,
    lido a cada volta); (2) gate de PLATAFORMA-NOVA pula cursos sem adaptador; (3) AUTÓPSIA
    dos cursos que morreram desde o último ciclo (P3->P4); (4) delega os demais ao OWNER
    `orquestrar_captura` com a passada LOCAL (disjuntor no gate de re-tentativa).

    A serialização anti-ban 1-por-conta é EMERGENTE (o executor levanta `ContaOcupada`). A
    contagem-verdade do Notion é lida UMA vez por curso por ciclo (cache)."""
    if agora is None:
        agora = time.time()
    controle = controle or _ControleNulo()
    disjuntor = disjuntor or _DisjuntorTeto(max_tentativas)
    vigia = vigia or _VigiaNulo()
    causa = causa or _CausaNula()
    alertas = alertas or _AlertasNulo()
    if flap_min is None:
        flap_min = _FLAP_MIN_PADRAO
    if meta_por_curso is None:
        meta_por_curso = {c.url: c for c in cursos}

    cache = {}

    def progresso_cached(curso):
        if curso not in cache:
            try:
                cache[curso] = ("ok", progresso_fn(curso))
            except Exception as e:                         # erro cacheado: 1 leitura/ciclo
                cache[curso] = ("err", e)
        kind, val = cache[curso]
        if kind == "err":
            raise val
        return val

    # (1) CONTROLE (P5): remove cursos de plataforma/conta PAUSADA — o daemon PARA de
    # RE-disparar aquela plataforma no próximo ciclo. Processo VIVO nunca é morto (Ordem IV).
    # `controle.yaml` corrompido => `filtrar_cursos` LEVANTA (fail-LOUD) e o ciclo estoura
    # (o `rodar` escala honesto). Só filtra quando há um path de controle (senão: passthrough).
    if controle_path is not None:
        cursos = controle.filtrar_cursos(cursos, controle_path)

    # (2) gate de PLATAFORMA-NOVA.
    cursos_ok = []
    for c in cursos:
        st = estado.setdefault(c.url, {})
        if (plataformas_suportadas is not None and
                not adaptador_pipeline.plataforma_suportada(c.url, plataformas_suportadas)):
            _escalar_plataforma_nova(projeto_nome, voz, c.url, st)
            continue
        cursos_ok.append(c.url)

    # (3) AUTÓPSIA dos mortos (P3 -> P4): antes das passadas, para que a decisão da causa
    # (backoff vs irredutível) já esteja no `st` quando a passada consultar o disjuntor.
    _autopsiar_ciclo(executor, estado, vigia=vigia, causa=causa, disjuntor=disjuntor,
                     alertas=alertas, lock_dir=lock_dir, autopsia_dir=autopsia_dir,
                     agora=agora, meta_por_curso=meta_por_curso, flap_min=flap_min, llm=llm)

    # (4) passada LOCAL + owner.
    passada = _passada_local_fn(executor, progresso_cached, voz, estado,
                                projeto_nome=projeto_nome, disjuntor=disjuntor, agora=agora)

    def notion_fn(curso):
        try:
            no_notion, total = progresso_cached(curso)
        except Exception:
            return (None, 0)
        return (no_notion, total)

    return orquestrador.orquestrar_captura(cursos_ok, passada, notion_fn, voz, voo,
                                           agora=agora)


def _escrever_pulso(path, *, ts, ciclo, ativos):
    """Grava o PULSO de vida do daemon ({ts, ciclo, ativos}) de forma atômica (tmp+replace).
    É a prova EXTERNA (fora da memória do processo) de que o loop está VIVO e avançando — o
    que o VIGIA EXTERNO (P6, scripts/vigia_externo.sh) lê para decidir se a Athena travou."""
    import json
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump({"ts": ts, "ciclo": ciclo, "ativos": list(ativos)}, f)
    os.replace(tmp, path)


async def rodar(cursos, executor, progresso_fn, voz, *, sleep=asyncio.sleep,
                intervalo_s=120.0, max_iters=None, plataformas_suportadas=None,
                max_tentativas=3, projeto_nome="athena-local", controle=None,
                controle_path=None, disjuntor=None, vigia=None, causa=None, alertas=None,
                batimento=None, batimento_intervalo=1800.0, resumo_fn=None, pulso_path=None,
                lock_dir=None, autopsia_dir=None, meta_por_curso=None, flap_min=None,
                llm=None):
    """O LOOP doméstico. Cria `voo` e `estado` UMA vez e os REINJETA a cada ciclo. Um ciclo
    que estoura NÃO derruba o loop, mas a falha é ESCALADA (latch por assinatura). A cada
    ciclo grava o PULSO e chama o BATIMENTO — ambos BEST-EFFORT (observabilidade nunca mata
    o loop)."""
    estado = {}
    voo = {}
    controle = controle or _ControleNulo()
    disjuntor = disjuntor or _DisjuntorTeto(max_tentativas)
    vigia = vigia or _VigiaNulo()
    causa = causa or _CausaNula()
    alertas = alertas or _AlertasNulo()
    batimento = batimento or _BatimentoNulo()
    if meta_por_curso is None:
        meta_por_curso = {c.url: c for c in cursos}
    ultimo_erro = None
    ultimo_batimento = 0.0
    t0 = time.time()
    i = 0
    while max_iters is None or i < max_iters:
        i += 1
        try:
            ciclo_local(cursos, executor, progresso_fn, voz, voo, estado,
                        agora=time.time(), plataformas_suportadas=plataformas_suportadas,
                        max_tentativas=max_tentativas, projeto_nome=projeto_nome,
                        controle=controle, controle_path=controle_path, disjuntor=disjuntor,
                        vigia=vigia, causa=causa, alertas=alertas, lock_dir=lock_dir,
                        autopsia_dir=autopsia_dir, meta_por_curso=meta_por_curso,
                        flap_min=flap_min, llm=llm)
            ultimo_erro = None                             # ciclo passou: re-arma o latch
        except Exception as e:
            assinatura = f"{type(e).__name__}:{str(e)[:120]}"
            if assinatura != ultimo_erro:                  # episódio novo -> escala honesto
                pedido = (f"[{projeto_nome}] o CICLO doméstico ESTOUROU (não derrubo o "
                          f"loop, mas NÃO capturo nada até resolver): {assinatura}")
                try:
                    voz.escalar(Problema("ciclo_local_estourou", projeto_nome, pedido,
                                         "critico"), pedido)
                except Exception:
                    pass                                   # a voz falhar não pode matar o loop
                ultimo_erro = assinatura
        # PULSO (P6 backstop lê isto) — best-effort, gravado MESMO num ciclo que estourou.
        if pulso_path:
            try:
                ativos = [c.url for c in cursos if _safe_ativo(executor, c.url)]
                _escrever_pulso(pulso_path, ts=time.time(), ciclo=i, ativos=ativos)
            except Exception:
                pass
        # BATIMENTO (P2) — pulso de vida no Telegram (mudo: só loga). Best-effort.
        def _resumo():
            if resumo_fn is not None:
                return resumo_fn()
            n = sum(1 for c in cursos if _safe_ativo(executor, c.url))
            return f"{n} captura(s) ativa(s), ciclo {i}, uptime {int(time.time() - t0)}s"
        try:
            ultimo_batimento = batimento.talvez_bater(
                voz, _resumo, time.time(), ultimo_batimento, batimento_intervalo)
        except Exception:
            pass
        await sleep(intervalo_s)
    return i


# ---------------------------------------------------------------------------
# Contagem-verdade do Notion LOCAL (I/O real; testada pelo seam `run`).
# ---------------------------------------------------------------------------
def _run_local(cmd, *, cwd):  # pragma: no cover — subprocesso REAL
    import subprocess
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=120).stdout


def contar_no_notion_local(motor_python, motor_dir, curso_url, *, run=None) -> int:
    """Conta as aulas deste curso já no Notion (por prefixo de 'Origem'), rodando o
    MESMO script do adaptador (`captura._PROGRESSO_SCRIPT`) LOCALMENTE (motor_python -c),
    com cwd=motor_dir para o `motor.config` carregar o NOTION_TOKEN do .env. LEVANTA se a
    sentinela não vier (silêncio NÃO vira 0 — subestimar re-capturaria; o chamador escala)."""
    run = run or _run_local
    prefixo = captura._prefixo_de_curso(curso_url)
    script = "import motor.config  # carrega .env (NOTION_TOKEN etc.)\n" + captura._PROGRESSO_SCRIPT
    cmd = [motor_python, "-c", script, prefixo]
    saida = run(cmd, cwd=motor_dir) or ""
    if captura.PROGRESSO_SENTINELA not in saida:
        raise RuntimeError(
            f"contagem LOCAL no Notion SEM confirmação ({captura.PROGRESSO_SENTINELA} "
            f"ausente) p/ {curso_url}: {saida[-160:]!r}")
    linha = next(l for l in saida.splitlines() if captura.PROGRESSO_SENTINELA in l)
    return int(linha.split(captura.PROGRESSO_SENTINELA, 1)[1].strip().split()[0])


def progresso_local_fn(motor_python, motor_dir, total_por_curso, *, run=None):
    """Fábrica da `progresso_fn` doméstica -> (no_notion, total). Numerador = contagem-
    verdade LOCAL do Notion; denominador = `total_por_curso` (o `total_esperado` do YAML).
    Curso sem total => 0 (o owner não conclui: fail-closed)."""
    def _fn(curso_url):
        no_notion = contar_no_notion_local(motor_python, motor_dir, curso_url, run=run)
        return (no_notion, int(total_por_curso.get(curso_url, 0)))
    return _fn


def carregar_cursos(path) -> list:
    """Lê a lista de cursos desejados do YAML doméstico. Cada entrada:
    {url, conta, plataforma?, total_esperado?}. `conta` é OBRIGATÓRIA (chave anti-ban) — a
    ausência LEVANTA (fail-closed)."""
    import yaml
    with open(path) as f:
        dados = yaml.safe_load(f) or []
    out = []
    for d in dados:
        out.append(captura.CursoLocal(
            url=d["url"], conta=d["conta"],
            plataforma=d.get("plataforma", "hotmart"),
            total_esperado=int(d.get("total_esperado", 0))))
    return out


def _motor_dirs_por_plataforma() -> dict:
    """Overrides de diretório do motor POR PLATAFORMA, lidos de env ATHENA_MOTOR_DIR_<PLAT>."""
    prefixo = "ATHENA_MOTOR_DIR_"
    return {k[len(prefixo):].lower(): v for k, v in os.environ.items()
            if k.startswith(prefixo) and v}


def _ler_groq_key(motor_dir) -> str:  # pragma: no cover — I/O real (lê o chave-groq.txt)
    """GROQ_API_KEY p/ INJETAR no subprocesso do motor (Stoa roda com cwd=worktree, sem
    chave-groq.txt lá). Lê de env GROQ_API_KEY ou do chave-groq.txt do `motor_dir`."""
    import re
    v = os.getenv("GROQ_API_KEY")
    if v:
        return v
    try:
        with open(os.path.join(motor_dir, "chave-groq.txt")) as f:
            m = re.search(r"gsk_[A-Za-z0-9]{20,}", f.read())
            return m.group(0) if m else ""
    except OSError:
        return ""


def _montar_alertas(cfg):  # pragma: no cover — I/O real (constrói o canal de supervisão)
    """Constrói os Alertas typados (P2) FAIL-SAFE: token MUDO/ausente => modo só-log (o
    canal que avisa que o supervisor caiu NUNCA pode ser quem o derruba). Com token real,
    posta no Telegram reusando o mesmo TelegramClient da Voz."""
    from maestro.alertas import Alertas
    from maestro.telegram_api import TelegramClient
    token = (cfg.bot_token or "").strip()
    if not token or token == "MUTED":
        return Alertas(None, list(cfg.chat_ids))
    return Alertas(TelegramClient(token), list(cfg.chat_ids))


def main():  # pragma: no cover — I/O real (monta os seams concretos e roda o loop)
    from maestro import batimento as batimento_mod
    from maestro import causa as causa_mod
    from maestro import controle as controle_mod
    from maestro import disjuntor as disjuntor_mod
    from maestro import vigia as vigia_mod
    from maestro.config import carregar
    from maestro.telegram_api import TelegramClient
    from maestro.voz import Voz

    cfg = carregar()
    voz = Voz(TelegramClient(cfg.bot_token), cfg.chat_ids)
    alertas = _montar_alertas(cfg)

    motor_python = os.getenv(
        "ATHENA_MOTOR_PYTHON", "/Users/guilhermerodrigues/teste/aula/.venv/bin/python")
    motor_dir = os.getenv("ATHENA_MOTOR_DIR", "/Users/guilhermerodrigues/teste/aula")
    cursos_path = os.environ["ATHENA_LOCAL_CURSOS"]
    cursos = carregar_cursos(cursos_path)

    lock_dir = os.getenv("ATHENA_LOCK_DIR") or None
    groq_key = _ler_groq_key(motor_dir) or None
    executor = captura.LocalExecutor(
        cursos, motor_python=motor_python, motor_dir=motor_dir, lock_dir=lock_dir,
        groq_key=groq_key, motor_dir_por_plataforma=_motor_dirs_por_plataforma())
    total_por_curso = {c.url: c.total_esperado for c in cursos}
    progresso_fn = progresso_local_fn(motor_python, motor_dir, total_por_curso)

    plataformas = frozenset(
        p for p in os.getenv(
            "PLATAFORMAS_SUPORTADAS",
            "hotmart.com,memberkit.com.br,stoa.com.br,mykajabi.com")
        .replace(" ", "").split(",") if p)
    plataformas_suportadas = plataformas or None
    max_tentativas = int(os.getenv("ATHENA_MAX_TENTATIVAS", "3"))

    # Caminhos duráveis das 6 partes (todos sob ~/.athena-local por padrão, o mesmo home
    # do lock_dir anti-ban — sobrevive a reboot).
    base = os.path.join(os.path.expanduser("~"), ".athena-local")
    controle_path = os.getenv("ATHENA_CONTROLE_PATH", os.path.join(base, "controle.yaml"))
    autopsia_dir = os.getenv("ATHENA_AUTOPSIA_DIR", os.path.join(base, "autopsias"))
    pulso_path = os.getenv("ATHENA_PULSO_PATH", os.path.join(base, "pulso.json"))
    lock_dir_efetivo = lock_dir or os.path.join(base, "locks")
    batimento_intervalo = float(os.getenv("ATHENA_BATIMENTO_S", "1800"))

    asyncio.run(rodar(
        cursos, executor, progresso_fn, voz, intervalo_s=cfg.intervalo_s,
        max_tentativas=max_tentativas, plataformas_suportadas=plataformas_suportadas,
        controle=controle_mod, controle_path=controle_path, disjuntor=disjuntor_mod,
        vigia=vigia_mod, causa=causa_mod, alertas=alertas, batimento=batimento_mod,
        batimento_intervalo=batimento_intervalo, pulso_path=pulso_path,
        lock_dir=lock_dir_efetivo, autopsia_dir=autopsia_dir,
        llm=causa_mod.seam_claude_p))


if __name__ == "__main__":  # pragma: no cover
    main()
