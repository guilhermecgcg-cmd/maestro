"""CABEÇA DE CAPTURA — a Athena ORQUESTRA as passadas de um curso até 100% MEDIDO NO
NOTION, com um DISJUNTOR que separa 'parede real anti-ban' de 'aula não-vídeo'.

Por que existe (P2 da spec): sem isto, o humano é o orquestrador — ele olha o que
sobrou de uma passada e dispara a próxima na mão, curso a curso. Um curso quase nunca
fica pronto numa passada só: a captura é uma CASCATA de 4 passadas sobre a MESMA lista
de aulas, cada uma pegando o que a anterior não pegou:

    1. legenda  (WebVTT)          — a captura-base; quem a dispara é `captura.coordenar`
    2. áudio    (Whisper)         — resolve aulas que ficaram `sem_legenda`
    3. embed    (Vimeo/HLS)       — resolve aulas que ficaram `sem_video`
    4. não-vídeo(documento/texto) — resolve aulas que ficaram `sem_embed`/`sem_audio`

Esta cabeça lê o CENSO por-estado do curso (do TRACKER/estado_aulas) para ESTRUTURAR a
decisão — há parede? falta disparar uma passada? — decide a PRÓXIMA passada da cascata e
dispara-a por um SEAM residencial (`disparar_passe`). Mas o censo é FLAG (o motor ACHA
que escreveu no Notion): declarar 100% por ele seria o falso-pronto. Por isso a conclusão
passa por um GATE que CRUZA a contagem REAL do Notion (`progresso_notion_fn`), a MESMA
régua do gate I-1 em `captura.coordenar` (completo <=> no_notion >= total). Ela NÃO abre
Chrome nem roda captura — delega, igual `coordenar` (inviolável anti-ban: browser só no
residencial).

────────────────────────────────────────────────────────────────────────────────────
O DISJUNTOR (o núcleo, `classificar`): duas coisas "não geraram vídeo" e são OPOSTAS —
    • PAREDE real anti-ban (`falhou`/`audio_erro`): a aula TEM conteúdo, a captura foi
      BLOQUEADA. Contar como 'feito' é o FALSO-PRONTO que o usuário odeia. → NÃO conta
      como done, NÃO deixa declarar 100%, ESCALA (anti-ban = decisão dele; esperar é
      decisão). E não dispara mais passadas com a parede acesa (não piora o ban).
    • AULA não-vídeo (`sem_embed`/`sem_audio` depois da passada não-vídeo ter rodado):
      legitimamente não tem vídeo (documento/texto). Contar como FALHA faria o curso
      nunca chegar a 100% e escalaria anti-ban à toa. → conta como done.
Trocar um pelo outro é exatamente o bug 'parede vira sucesso'. O disjuntor é o que os
mantém separados; os testes têm DENTES nos dois sentidos.
"""
import time

from maestro.playbook import Acao
from maestro.sentinela import Problema


# --- AS 4 PASSADAS DA CASCATA (em ordem) ------------------------------------
PASSE_LEGENDA = "legenda"      # 1: captura-base (WebVTT) — disparada por coordenar
PASSE_AUDIO = "audio"          # 2: áudio/Whisper — para sem_legenda
PASSE_EMBED = "embed"          # 3: embed/Vimeo HLS — para sem_video
PASSE_NAO_VIDEO = "nao_video"  # 4: documento/texto — para sem_embed/sem_audio
PASSES_ORDEM = (PASSE_LEGENDA, PASSE_AUDIO, PASSE_EMBED, PASSE_NAO_VIDEO)
# passadas que ESTA cabeça dispara (a legenda é da captura-base, não daqui).
PASSES_EXTRAS = (PASSE_AUDIO, PASSE_EMBED, PASSE_NAO_VIDEO)

# --- CLASSES do disjuntor ----------------------------------------------------
CLASSE_CAPTURADA = "capturada"   # em Notion (no_notion/anexos_baixados) — a medida de verdade
CLASSE_PAREDE = "parede"         # falha real / anti-ban (falhou/audio_erro) — bloqueia 100%
CLASSE_PENDENTE = "pendente"     # ainda precisa de uma passada, ou captura-base em voo

ORQ_CONCLUIDO = "concluido"      # fase do estado por-curso: 100% medido e declarado

# --- O MAPA estado-do-tracker -> (classe, passada_que_resolve, endpoint_nao_video) ----
# DOIS eixos INDEPENDENTES para um estado 'sem X' pendente:
#   • `passada_que_resolve` (2º campo, != None): a passada da cascata que o DISPARA.
#     TODOS os quatro 'sem X' têm uma — todos são disparáveis.
#   • `endpoint_nao_video` (3º campo): SÓ True nos endpoints da cascata (sem_embed/
#     sem_audio, que a passada nao_video resolve). DEPOIS que a passada rodou e o estado
#     seguiu 'sem X', um endpoint vira NÃO-VÍDEO legítima (done). NÃO-endpoints
#     (sem_legenda/sem_video) são estados INTERMEDIÁRIOS: a aula TEM vídeo (só falta
#     transcrição/embed). Se o motor a deixa presa ali depois da passada, contá-la como
#     done seria FALSO-PRONTO — então ela BLOQUEIA (fail-closed), nunca completa sozinha.
# Por que separar: o disjuntor canônico (docstring do módulo) diz que SÓ sem_embed/
# sem_audio, após a passada nao_video, são não-vídeo. Marcar sem_legenda/sem_video como
# 'done' contradiz isso e abre o falso-pronto. Estados em VOO/desconhecidos não são nem
# disparáveis nem endpoint (passada None, endpoint False): bloqueiam honestamente.
# Fonte dos nomes: aula/motor/tracker.py (mesma lista de captura.ESTADOS_*).
_MAPA = {
    # sucesso terminal = provado no Notion
    "no_notion": (CLASSE_CAPTURADA, None, False),
    "anexos_baixados": (CLASSE_CAPTURADA, None, False),
    # falhas reais = PAREDE (nunca 'feito')
    "falhou": (CLASSE_PAREDE, None, False),
    "audio_erro": (CLASSE_PAREDE, None, False),
    # 'sem X' INTERMEDIÁRIOS: disparáveis, mas NÃO endpoint — têm vídeo, nunca viram done
    "sem_legenda": (CLASSE_PENDENTE, PASSE_AUDIO, False),
    "sem_video": (CLASSE_PENDENTE, PASSE_EMBED, False),
    # 'sem X' ENDPOINT da cascata: após a passada nao_video, são não-vídeo legítima (done)
    "sem_embed": (CLASSE_PENDENTE, PASSE_NAO_VIDEO, True),
    "sem_audio": (CLASSE_PENDENTE, PASSE_NAO_VIDEO, True),
    # passada de embed TRABALHANDO: em voo, aguarda (não é 'sem X', não vira done)
    "transcrevendo_embed": (CLASSE_PENDENTE, None, False),
}


def classificar(estado: str):
    """DISJUNTOR (núcleo): estado -> (classe, passada_que_resolve, endpoint_nao_video).

    O 2º campo (!= None) diz qual passada DISPARA o estado; o 3º diz se, exaurida a
    passada, ele vira NÃO-VÍDEO legítima (só sem_embed/sem_audio). Estado desconhecido/
    não-terminal cai no default SEGURO: PENDENTE, passada None (captura-base em voo),
    endpoint False. NUNCA CAPTURADA nem PAREDE — um estado que o motor invente não pode
    nem virar falso-pronto (done sem prova) nem falso anti-ban (escalar à toa). Fica
    bloqueando honestamente até virar um estado conhecido."""
    return _MAPA.get(estado, (CLASSE_PENDENTE, None, False))


class Diagnostico:
    """Retrato do curso a partir do censo por-estado + as passadas já disparadas.

    - `capturadas`: aulas provadas no Notion (a medida de 100%).
    - `nao_video`: aulas 'sem X' cuja passada já teve a chance e seguem sem vídeo —
       legítimas, contam como done (o outro lado do disjuntor).
    - `paredes`: falhas reais / anti-ban — bloqueiam o 100% (matam o falso-pronto).
    - `em_voo`: aulas em estado não-terminal / passada trabalhando — captura-base ainda
       rodando; bloqueia, mas NÃO é disparável por esta cabeça.
    - `pendentes`: [(passada, contagem)] das passadas EXTRAS que precisam ser disparadas.
    - `proximo_passe`: a passada extra mais cedo na cascata com pendência (ou None)."""

    __slots__ = ("total", "capturadas", "nao_video", "paredes", "em_voo",
                 "pendentes", "proximo_passe")

    def __init__(self, total, capturadas, nao_video, paredes, em_voo,
                 pendentes, proximo_passe):
        self.total = total
        self.capturadas = capturadas
        self.nao_video = nao_video
        self.paredes = paredes
        self.em_voo = em_voo
        self.pendentes = pendentes
        self.proximo_passe = proximo_passe

    @property
    def completo(self) -> bool:
        """100% MEDIDO: total>0, ZERO parede, ZERO pendência/voo, e tudo que existe é
        capturada-no-Notion ou não-vídeo-legítima. Fail-closed: total 0 nunca prova."""
        return (self.total > 0 and self.paredes == 0 and self.em_voo == 0
                and not self.pendentes
                and (self.capturadas + self.nao_video) == self.total)


def diagnosticar(estados: dict, passes_disparados=()) -> Diagnostico:
    """Aplica o disjuntor a cada bucket do censo e soma o retrato do curso.

    Uma aula 'sem X' disparável (2º campo != None):
      - se a passada que a resolve AINDA não foi disparada -> PENDENTE (dispare-a);
      - se a passada JÁ foi disparada e ela segue 'sem X':
          · ENDPOINT (sem_embed/sem_audio, endpoint True) -> teve a chance, é não-vídeo
            legítima (done). Impede o falso-negativo (não-vídeo travando para sempre).
          · INTERMEDIÁRIO (sem_legenda/sem_video, endpoint False) -> a aula TEM vídeo;
            marcá-la done seria FALSO-PRONTO. BLOQUEIA (em_voo), fail-closed. Um motor
            sadio já a teria movido adiante; presa aqui é anomalia que NÃO se completa.
    Parede é classe à parte (nunca disparável aqui), então nunca cai neste ramo."""
    disparados = set(passes_disparados)
    total = cap = nao_video = paredes = em_voo = 0
    pend = {p: 0 for p in PASSES_EXTRAS}
    for est, n in estados.items():
        n = int(n)
        total += n
        classe, passe, endpoint = classificar(est)
        if classe == CLASSE_CAPTURADA:
            cap += n
        elif classe == CLASSE_PAREDE:
            paredes += n
        elif passe is not None:
            # 'sem X' disparável: done só se ENDPOINT e a passada já rodou; senão pendente
            # pra disparar; se INTERMEDIÁRIO persistindo pós-passada, bloqueia (fail-closed).
            if passe in disparados:
                if endpoint:
                    nao_video += n
                else:
                    em_voo += n
            else:
                pend[passe] += n
        else:
            # captura-base em voo / passada trabalhando / estado desconhecido: aguarda.
            em_voo += n
    proximo = next((p for p in PASSES_EXTRAS if pend[p] > 0), None)
    pendentes = tuple((p, pend[p]) for p in PASSES_EXTRAS if pend[p] > 0)
    return Diagnostico(total=total, capturadas=cap, nao_video=nao_video,
                       paredes=paredes, em_voo=em_voo, pendentes=pendentes,
                       proximo_passe=proximo)


def orquestrar(projeto, voz, *, censo_fn, disparar_passe, curso_url, estado,
               agora=None, progresso_notion_fn=None):
    """Um passo por ciclo da CABEÇA DE CAPTURA de UM curso. Lê o censo (verdade), aplica
    o disjuntor e faz UMA coisa: escala parede, OU dispara a próxima passada, OU declara
    100%, OU aguarda. Avança a máquina de estados em `estado` (dict por curso, persiste
    entre ciclos). Dono do próprio reporte (dirige a `voz`); devolve a Acao (ou None).

    Seams (NUNCA roda captura de verdade):
      - `censo_fn(curso_url) -> dict[estado, contagem]`: o retrato por-estado do TRACKER
        (estado_aulas). Serve para ESTRUTURAR a decisão (parede? passada pendente?), mas
        'capturada' aqui é o STATUS que o motor ACHA que escreveu — não é prova.
      - `disparar_passe(passe, curso_url) -> confirmação truthy`: enfileira a passada no
        residencial (nunca Chrome na VPS). Levanta/retorna falsy em falha -> escala honesto.
      - `progresso_notion_fn(curso_url) -> ProgressoNotion`: a CONTAGEM REAL de aulas do
        curso já no Notion — a MESMA prova que `captura.coordenar` usa no gate I-1. É o
        que autoriza declarar 100%. None => FAIL-CLOSED: NÃO declara pronto (não confia
        no flag do tracker); produção DEVE ligar este seam (mesma régua em captura.py e
        aqui: completo <=> no_notion >= total).

    INVIOLÁVEIS cravados aqui:
      - PAREDE anti-ban -> não declara pronto (mata o falso-pronto) e segura a captura;
      - captura sempre pelo seam residencial (anti-ban);
      - 100% só quando o NOTION PROVA (I-1: no_notion >= total), NUNCA por flag do tracker;
      - nenhuma passada é dada como disparada sem confirmação."""
    agora = time.time() if agora is None else agora
    if estado.get("orq_fase") == ORQ_CONCLUIDO:
        return None                                        # idempotente: nada a fazer

    try:
        estados = censo_fn(curso_url)
    except Exception as e:
        pedido = (f"[{projeto.nome}] não consigo ler o censo de estados de {curso_url} "
                  f"(cabeça de captura): {str(e)[:140]}")
        voz.escalar(Problema("censo_inacessivel", curso_url, pedido, "aviso"), pedido)
        return Acao("", False, True, pedido)

    disparados = set(estado.get("passes_disparados", ()))
    diag = diagnosticar(estados, disparados)

    # ---- DISJUNTOR / CIRCUIT BREAKER: parede real anti-ban -----------------
    # Segura tudo: NÃO declara pronto (falso-pronto), NÃO dispara mais passadas (não
    # piora o ban) e escala uma decisão do humano (reseed/retry). Reporta UMA vez por
    # parede (latch em `estado`) pra não spammar o Telegram.
    if diag.paredes > 0:
        if not estado.get("parede_reportada"):
            pedido = (f"[{projeto.nome}] PAREDE anti-ban em {curso_url}: {diag.paredes} "
                      f"aula(s) em falha real (falhou/audio_erro) — NÃO declaro pronto "
                      f"(evito falso-pronto), seguro a captura; preciso de decisão "
                      f"(reseed do storage_state / retry)")
            voz.escalar(Problema("parede_anti_ban", curso_url, pedido, "critico"), pedido)
            estado["parede_reportada"] = True
        return Acao("", False, True,
                    f"[{projeto.nome}] {curso_url}: {diag.paredes} parede(s) anti-ban "
                    f"— não declaro pronto")
    estado["parede_reportada"] = False                     # parede sumiu -> destrava o reporte

    # ---- PRÓXIMA PASSADA da cascata (uma por ciclo) ------------------------
    if diag.proximo_passe is not None:
        try:
            conf = disparar_passe(diag.proximo_passe, curso_url)
        except Exception as e:
            pedido = (f"[{projeto.nome}] FALHEI ao disparar a passada "
                      f"'{diag.proximo_passe}' de {curso_url}: {str(e)[:140]}")
            voz.escalar(Problema("passe_disparo_falhou", curso_url, pedido, "critico"),
                        pedido)
            return Acao("", False, True, pedido)
        if not conf:
            pedido = (f"[{projeto.nome}] passada '{diag.proximo_passe}' de {curso_url} "
                      f"SEM confirmação do executor residencial — não assumo sucesso")
            voz.escalar(Problema("passe_sem_confirmacao", curso_url, pedido, "critico"),
                        pedido)
            return Acao("", False, True, pedido)
        disparados.add(diag.proximo_passe)
        estado["passes_disparados"] = sorted(disparados)   # persiste (JSON-friendly)
        acao = Acao(f"[{projeto.nome}] passada '{diag.proximo_passe}' disparada p/ "
                    f"{curso_url}: {conf}", True, False)
        voz.avisar_acao(acao)
        return acao

    # ---- 100% — só declara com a VERDADE do Notion (gate I-1), nunca por flag ----
    # O censo (tracker/estado_aulas) diz que ESTRUTURALMENTE acabou: sem parede, sem
    # passada pendente, tudo é capturada-claim ou não-vídeo-claim. Mas 'capturada' é o
    # STATUS que o motor ACHA que escreveu no Notion — a MESMA fonte-flag que já mentiu
    # pronto (é por isso que captura.coordenar tem o gate I-1). Antes de gravar
    # ORQ_CONCLUIDO, CRUZA contra a contagem REAL do Notion, a MESMA régua de captura.py:
    # no_notion >= total (diag.total). Isto mata DOIS falsos-prontos:
    #   [3] conclusão por FLAG do tracker (censo diz capturada mais do que o Notion tem);
    #   [4] não-vídeo contado por passada apenas ENFILEIRADA e ainda não processada — as
    #       aulas ainda não estão no Notion, então no_notion < total: NÃO declaramos 100%
    #       nem LATCHAMOS ORQ_CONCLUIDO, e o ciclo seguinte RE-OBSERVA (uma parede que
    #       apareça depois é vista e escalada, não engolida por um latch prematuro).
    if diag.completo:
        if progresso_notion_fn is None:
            # Sem a costura de verdade não dá para PROVAR completude -> FAIL-CLOSED: NÃO
            # declara 100% (declarar por flag do tracker é o falso-pronto do Inviolável 4).
            # Reporta uma vez (latch) e aguarda; produção DEVE ligar progresso_notion_fn.
            if not estado.get("conclusao_sem_prova_reportada"):
                pedido = (f"[{projeto.nome}] {curso_url}: censo diz 100% mas falta a costura "
                          f"de contagem no Notion (progresso_notion_fn) — NÃO declaro pronto "
                          f"por flag do tracker (Inviolável 4)")
                voz.escalar(Problema("conclusao_sem_prova_notion", curso_url, pedido, "aviso"),
                            pedido)
                estado["conclusao_sem_prova_reportada"] = True
            return Acao("", False, True,
                        f"[{projeto.nome}] {curso_url}: conclusão retida (sem prova no Notion)")
        try:
            prog_notion = progresso_notion_fn(curso_url)
        except Exception as e:
            pedido = (f"[{projeto.nome}] {curso_url}: censo diz 100% mas NÃO consegui "
                      f"confirmar no Notion (gate I-1): {str(e)[:140]} — NÃO declaro pronto")
            voz.escalar(Problema("conclusao_notion_inacessivel", curso_url, pedido, "aviso"),
                        pedido)
            return Acao("", False, True, pedido)
        from maestro import auditor
        laudo = auditor.auditar_conclusao(curso_url, prog_notion, diag.total)
        if not laudo.aprovado:
            # FALSO-PRONTO: o tracker diz 100% mas o Notion prova menos (flag mentiu, ou
            # não-vídeo ainda não processado). NÃO declara, NÃO latcha (re-observa). Reporta
            # uma vez (latch de reporte), destravando quando o Notion alcançar o total.
            falta = max(diag.total - prog_notion.no_notion, 0)
            if not estado.get("falso_pronto_reportado"):
                pedido = (f"[{projeto.nome}] {curso_url} censo diz 100% mas o Notion tem "
                          f"{prog_notion.no_notion}/{diag.total} — {falta} faltando "
                          f"(falso-pronto REJEITADO, I-1); NÃO declaro pronto")
                voz.escalar(Problema("falso_pronto", curso_url, pedido, "critico"), pedido)
                estado["falso_pronto_reportado"] = True
            return Acao("", False, True,
                        f"[{projeto.nome}] {curso_url}: conclusão REJEITADA — Notion tem "
                        f"{prog_notion.no_notion}/{diag.total} ({falta} faltando)")
        estado["falso_pronto_reportado"] = False
        estado["orq_fase"] = ORQ_CONCLUIDO
        acao = Acao(f"[{projeto.nome}] {curso_url} 100% capturado e PROVADO no Notion "
                    f"({prog_notion.no_notion}/{diag.total}; {diag.capturadas} capturada(s) + "
                    f"{diag.nao_video} não-vídeo, 0 parede) — todas as passadas concluídas",
                    True, False)
        voz.avisar_acao(acao)
        return acao

    # ---- aguarda: captura-base em voo / censo vazio -> quieto --------------
    return None


# --- SEAM REAL: leitura do censo por-estado do tracker ----------------------
def _quote(valor) -> str:
    """Quota literal TEXT p/ SQL escapando aspas simples (course_id é TEXT, não int)."""
    return "'" + str(valor).replace("'", "''") + "'"


_SQL_CENSO = ("SELECT status, count(*) FROM estado_aulas "
              "WHERE course_id = {curso} GROUP BY status")


def censo_estados(projeto, acesso, course_id) -> dict:
    """Censo por-estado REAL do curso: {status: contagem}, lido do tracker (estado_aulas)
    agrupado por status, via `docker exec ... psql` (mesmo padrão de captura.progresso).
    É a fonte de verdade que `orquestrar` consome — o chamador liga este seam.

    Linha malformada (sem o separador '|') é ignorada, não derruba o censo. Sem linhas
    -> {} (curso não enumerado): o diagnóstico trata total 0 como fail-closed."""
    linhas = acesso.exec_sql(
        projeto.db_container, _SQL_CENSO.format(curso=_quote(course_id)),
        db=projeto.db_name, user=projeto.db_user)
    censo = {}
    for linha in linhas or []:
        parts = str(linha).split("|", 1)
        if len(parts) != 2:
            continue
        status = parts[0].strip()
        try:
            censo[status] = int(parts[1])
        except ValueError:
            continue
    return censo
