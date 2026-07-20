"""Adaptador de CAPTURA: ensina ao Maestro o PROTOCOLO DE CAPTURA de cursos. O
núcleo do Maestro é genérico (ops universal); este adaptador coordena, por curso,
as fases do protocolo — checar sessão, disparar a captura, monitorar o progresso e,
ao concluir, disparar o auto-ingest (Notion->pgvector) REUSANDO o adaptador irmão
`conhecimento.reconciliar`.

INVIOLÁVEIS (I-3 anti-ban / arquitetura decidida pelo usuário):
  1. NUNCA auto-login. Sessão morta -> PEDE reseed via `voz`, não tenta logar.
  2. A captura por browser roda em IP RESIDENCIAL (o Mac do usuário), NUNCA na VPS
     (datacenter = risco de ban). Por isso este adaptador NÃO abre Chrome: ele aciona
     um EXECUTOR INJETADO (ex.: enfileira em fila_captura, ou chama um callable
     Mac-side). O executor é o único ponto que sabe "como" a captura acontece.
  3. Escala honesta (I-1): etapa que falha ou não confirma sucesso reporta/escala via
     `voz` — nunca finge sucesso.

Acesso ao tracker é via `docker exec ... psql` (Acesso.exec_sql), mesmo padrão do
adaptador conhecimento: o Maestro roda num projeto Easypanel PRÓPRIO, isolado da
rede do conhecimento, e entra de DENTRO do container do banco (sem DNS interno).

IDENTIDADE (course_url vs course_id): o coordenador é dirigido por `course_url` — é o
que o usuário cadastra e o que a `fila_captura` chaveia (índice único parcial). O
`course_id` (TEXT, chave do `estado_aulas`) só existe DEPOIS que o worker residencial
reivindica o job e o resolve via `vincular_curso`. Logo: dispara-se por URL; monitora-
se por course_id resolvido da fila. Enquanto o worker não reivindicou, o course_id é
nulo e o coordenador fica "aguardando reivindicação" — sem erro, sem escalar.
"""
import shlex
import time
from dataclasses import dataclass

from maestro import observador
from maestro.adaptadores import conhecimento
from maestro.playbook import Acao
from maestro.sentinela import Problema

# --- CONTRATO com o WORKER RESIDENCIAL (worker-captura-vps) ------------------
# O Maestro roda na VPS SEM browser: ele NUNCA sonda a plataforma para saber se a
# sessão está viva. A liveness REAL só o worker residencial conhece — ele tem o
# storage_state JSON local. Portanto o único sinal de sessão morta que o Maestro
# consegue ler é o STATUS que o worker PUBLICA na fila_captura: ao reivindicar um job
# e bater num SessionLostError, o worker DEVE marcar o job com este status. Sem esse
# sinal, a sessão é 'desconhecida' (não 'viva': não dá para confirmar liveness daqui),
# e o Maestro SEGUE enfileirando — o worker é o detector real, no momento do claim.
# >>> Este é o contrato que o worker residencial precisa implementar. <<<
STATUS_SESSAO_MORTA = "sessao_morta"

# Fases do protocolo de captura de UM curso. Persistem em `estado` entre ciclos do
# loop (mesmo padrão do `ultimo` de reconciliar): o coordenador é chamado a cada
# ciclo e avança a máquina de estados sem bloquear.
FASE_NOVO = "novo"                # ainda não disparado
FASE_CAPTURANDO = "capturando"    # disparado no residencial; monitorando progresso
FASE_CONCLUIDO = "concluido"      # capturado + auto-ingest confirmado

# --- ESTADOS TERMINAIS DE UMA AULA (fonte: aula/motor/tracker.py) ------------
# "done" de um curso (I1) = NÃO há aula em estado NÃO-terminal. Terminal = a aula
# chegou a um repouso definitivo do ponto de vista da captura, seja sucesso
# (no_notion/anexos_baixados), benigno (sem_legenda/sem_video/sem_embed/sem_audio —
# aula sem esse insumo, tratada por passes próprios) ou falha determinística
# (audio_erro/falhou). `transcrevendo_embed` é o passe de embed em resume — não é
# produzido pela captura inicial, então não a mantém "presa". Definir "done" por
# terminal (e não por "== no_notion") evita que um curso com aulas sem_legenda/
# sem_video/falhou fique CAPTURANDO para sempre.
ESTADOS_TERMINAIS = (
    "no_notion", "anexos_baixados",                 # sucesso terminal
    "sem_legenda", "sem_video", "sem_embed", "sem_audio",  # terminais benignos
    "audio_erro", "falhou",                         # falhas terminais
    "transcrevendo_embed",                          # passe de embed em resume
)
_TERMINAIS_SQL = ", ".join("'" + s + "'" for s in ESTADOS_TERMINAIS)  # literais FIXOS


def _quote(valor) -> str:
    """Quota um literal TEXT para SQL escapando aspas simples. As chaves reais —
    course_url e course_id (estado_aulas.course_id é TEXT) — são TEXT, NÃO int; a
    defesa anti-injeção é o ESCAPING de aspas, não `int()` (que quebraria valores
    legítimos e não-numéricos como URLs ou ids alfanuméricos)."""
    return "'" + str(valor).replace("'", "''") + "'"


# --- QUERIES DO TRACKER -----------------------------------------------------
# Sinal de sessão: LÊ o STATUS publicado pelo worker na fila_captura (ver contrato
# acima). NÃO consulta `sessao_plataforma` — essa tabela NÃO existe no schema real.
_SQL_SESSAO_MORTA = (
    "SELECT 1 FROM fila_captura "
    "WHERE course_url = {url} AND status = {morta} LIMIT 1")

# Resolve o course_id que o worker gravou (vincular_curso) após reivindicar o job.
# coalesce -> '' distingue "job existe mas ainda sem course_id" de "sem job".
_SQL_RESOLVE_COURSE_ID = (
    "SELECT coalesce(course_id, '') FROM fila_captura "
    "WHERE course_url = {url} ORDER BY id DESC LIMIT 1")

# Progresso reusa a tabela REAL do conhecimento (estado_aulas). Conta total e
# PENDENTES (status NÃO-terminal). "done" do curso = pendentes == 0 (e total > 0).
_SQL_PROGRESSO = (
    "SELECT count(hash), "
    "count(hash) FILTER (WHERE status NOT IN (" + _TERMINAIS_SQL + ")) "
    "FROM estado_aulas WHERE course_id = {curso}")


def estado_sessao(projeto, acesso, curso_url) -> str:
    """'morta' | 'desconhecida'. NUNCA loga; NUNCA sonda a plataforma; só LÊ a fila.

    - 'morta': há um job deste curso em STATUS_SESSAO_MORTA (o worker publicou o sinal)
      -> escala reseed via voz. NÃO tenta logar (inviolável anti-login).
    - 'desconhecida': sem sinal de morte (ou exec falhou / sem db_container). NÃO
      bloqueia nem escala — o Maestro segue enfileirando; o worker residencial é o
      detector real da sessão (tem o storage_state local) no momento do claim.

    'viva' NÃO é derivável na VPS (sem browser, sem storage_state) — por isso não é
    retornada: afirmar liveness daqui seria mentira. A distinção que importa aqui é
    morta (sinal explícito -> age) vs desconhecida (sem sinal -> segue, não age às
    cegas em nenhuma direção).
    """
    if not getattr(projeto, "db_container", ""):
        return "desconhecida"
    try:
        linhas = acesso.exec_sql(
            projeto.db_container,
            _SQL_SESSAO_MORTA.format(url=_quote(curso_url),
                                     morta=_quote(STATUS_SESSAO_MORTA)),
            db=projeto.db_name, user=projeto.db_user)
    except Exception:
        return "desconhecida"
    return "morta" if linhas else "desconhecida"


def resolver_course_id(projeto, acesso, curso_url) -> str:
    """course_id (TEXT) que o worker resolveu para esta URL, ou '' se ainda não
    resolvido (job não reivindicado / sem course_id gravado). LEVANTA se o acesso
    falhar — o coordenador decide como reportar."""
    linhas = acesso.exec_sql(
        projeto.db_container,
        _SQL_RESOLVE_COURSE_ID.format(url=_quote(curso_url)),
        db=projeto.db_name, user=projeto.db_user)
    if not linhas:
        return ""
    return linhas[0].strip()


def progresso(projeto, acesso, course_id) -> tuple:
    """(total, done, pendentes) do curso, lido do tracker (estado_aulas) por course_id
    (TEXT, quotado). done = aulas em estado terminal; pendentes = não-terminais."""
    linhas = acesso.exec_sql(
        projeto.db_container, _SQL_PROGRESSO.format(curso=_quote(course_id)),
        db=projeto.db_name, user=projeto.db_user)
    if not linhas:
        return (0, 0, 0)
    parts = linhas[0].split("|", 1)
    try:
        total = int(parts[0])
        pend = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return (0, 0, 0)
    return (total, max(total - pend, 0), pend)


# --- GUARD ANTI-DUPLICIDADE (dono: ATHENA) ----------------------------------
# O usuário foi queimado por RE-capturar um curso já pronto (Invisto Direito: 485
# aulas já no Notion; uma re-enumeração sob um rótulo duplicado fez o sistema achar
# que estava pendente). Diretiva: a garantia de NÃO-duplicidade no INÍCIO da captura
# é da Athena e tem de sobreviver ao Mac desligar / a sessão acabar. Por isso a
# checagem consulta a FONTE DA VERDADE DURÁVEL (Notion) a CADA ciclo, inclusive após
# restart — nunca uma memória local (um set in-process morreria no restart e o curso
# pronto voltaria a parecer novo, o exato prejuízo).
#
# COMO ALCANÇA O NOTION (decisão de design):
# Rodamos a consulta DE DENTRO do container do app (`acesso.exec_app`), reusando o
# token do Notion que o app JÁ tem (NOTION_TOKEN / NOTION_DB_LESSONS_ID em env) e o
# `notion_client` já instalado lá — mesmíssimo padrão da ponte reconcile. Assim NÃO
# adicionamos segredo novo ao Maestro/Athena. O script imita `reconcile.enumerar_
# aulas_notion`: resolve o data_source_id do database e usa `data_sources.query`
# (notion-client v3), mas com um FILTRO por PREFIXO de URL da propriedade 'Origem'
# ('starts_with') — barato e preciso, sem ler blocos. Imprime a sentinela abaixo.
#
# POR QUE PREFIXO DE URL (não o rótulo de exibição): o rótulo fragmenta
# ("InvistoDireito" vs "Invisto Direito | Escola de PPS") e foi o que enganou o
# sistema. A URL não mente: as aulas de um curso têm URL que COMEÇA pela URL do curso
# (curso .../products/3486759 -> aulas .../products/3486759/content/XXX).
NOTION_SENTINELA = "JA_NO_NOTION"       # guard anti-duplicidade (tem/não tem)
# PROGRESSO usa o MESMO mecanismo de contagem por prefixo de 'Origem', só com uma
# sentinela própria (a Athena lê o done-count REAL do curso na fonte de verdade).
PROGRESSO_SENTINELA = "PROGRESSO_NOTION"


def _build_count_script(sentinela: str) -> str:
    """Constrói o script executado DENTRO do container do app (uv run python -c). O
    prefixo vai como ARGV (shlex-quotado no comando), nunca interpolado no fonte —
    evita quebrar aspas. Pagina com data_sources.query (page_size 100) até esgotar e
    imprime `<sentinela> <count>`. Único ponto que fala com o Notion — reusa o token
    que o app JÁ tem (NOTION_TOKEN / NOTION_DB_LESSONS_ID), sem segredo novo na Athena.
    Anti-dup e progresso partilham EXATAMENTE a mesma consulta (só muda a sentinela)."""
    return (
        "import os,sys\n"
        "from notion_client import Client\n"
        "n=Client(auth=os.environ['NOTION_TOKEN'])\n"
        "ds=n.databases.retrieve(database_id=os.environ['NOTION_DB_LESSONS_ID'])"
        "['data_sources'][0]['id']\n"
        "p=sys.argv[1];c=0;cur=None\n"
        "while True:\n"
        " kw={'data_source_id':ds,'filter':{'property':'Origem','url':{'starts_with':p}},"
        "'page_size':100}\n"
        " if cur: kw['start_cursor']=cur\n"
        " r=n.data_sources.query(**kw);c+=len(r.get('results',[]))\n"
        " if not r.get('has_more'): break\n"
        " cur=r.get('next_cursor')\n"
        "print('" + sentinela + " %d' % c)\n"
    )


_NOTION_COUNT_SCRIPT = _build_count_script(NOTION_SENTINELA)
_PROGRESSO_SCRIPT = _build_count_script(PROGRESSO_SENTINELA)


def _normalizar_url_curso(url: str) -> str:
    """Normaliza a URL do curso para servir de PREFIXO estável: tira espaços, a query
    string (`?access_source=...`, utm, etc.) e o fragmento (`#...`), e a barra final.
    As aulas NÃO carregam essa query — normalizar ANTES é o que faz o prefixo casar."""
    base = str(url).strip()
    base = base.split("#", 1)[0]
    base = base.split("?", 1)[0]
    return base.rstrip("/")


def _prefixo_de_curso(url: str) -> str:
    """Prefixo de MATCH: URL normalizada do curso + '/'. A barra é a FRONTEIRA que
    impede um curso .../3486759 casar as aulas de .../34867590 (um dígito a mais):
    '34867590/content/...' NÃO começa por '3486759/'. As aulas reais começam por
    '<curso>/content/...', então sempre casam este prefixo."""
    return _normalizar_url_curso(url) + "/"


def _contar_no_notion(acesso, alvo_container, curso_url, *, sentinela, script,
                      ctx="") -> int:
    """Núcleo COMPARTILHADO da contagem por prefixo de 'Origem' (anti-dup e progresso).
    Roda `script` DE DENTRO do container do app (exec_app), passando o prefixo do curso
    como ARGV, e devolve a contagem que a sentinela imprime.

    LEVANTA se não der para determinar (sem container, exec falha, ou saída sem a
    sentinela). Silêncio NÃO pode virar 0: para o anti-dup isso viraria 'curso novo'
    (re-captura), e para o progresso viraria 'nada no Notion' (subestima) — em ambos
    o chamador ESCALA honesto em vez de agir às cegas."""
    if not alvo_container:
        raise RuntimeError(
            f"{ctx}sem app_container: não dá para verificar o Notion de {curso_url}")
    prefixo = _prefixo_de_curso(curso_url)
    comando = ("uv run --directory /app python -c "
               f"{shlex.quote(script)} {shlex.quote(prefixo)}")
    saida = acesso.exec_app(alvo_container, comando) or ""
    if sentinela not in saida:
        raise RuntimeError(
            f"{ctx}contagem no Notion SEM confirmação ({sentinela} ausente) "
            f"p/ {curso_url}: {saida[-160:]!r}")
    linha = next(l for l in saida.splitlines() if sentinela in l)
    return int(linha.split(sentinela, 1)[1].strip().split()[0])


def curso_ja_no_notion(projeto, acesso, curso_url) -> tuple:
    """(tem_aulas, quantidade) — quantas aulas do Notion ('Aulas (motor)') têm 'Origem'
    cuja URL começa pelo prefixo deste curso. FONTE DA VERDADE durável e stateless: lê
    o Notion a cada chamada, não guarda nada em memória local.

    LEVANTA se não der para determinar (sem app_container, exec falha, ou saída sem a
    sentinela). Silêncio NÃO pode virar '(False,0)': isso viraria 'curso novo' e
    re-capturaria — o oposto do que este guard existe para evitar. O chamador
    (coordenar) captura a exceção e ESCALA honesto, sem enfileirar às cegas."""
    qtd = _contar_no_notion(acesso, getattr(projeto, "app_container", ""), curso_url,
                            sentinela=NOTION_SENTINELA, script=_NOTION_COUNT_SCRIPT,
                            ctx=f"[{projeto.nome}] (anti-duplicidade) ")
    return (qtd > 0, qtd)


# --- VISÃO-DE-PROGRESSO POR VERDADE (dono: ATHENA) --------------------------
# O #1 problema: o sistema reportou cursos 'pronto' que estavam incompletos. A cura é
# medir o done-count REAL na FONTE DE VERDADE (Notion), NUNCA num flag. `ProgressoNotion`
# carrega quantas aulas o Notion já tem (`no_notion`); `completo(total)` só diz "pronto"
# quando no_notion >= total (com total>0) — e o `total` (esperado) vem da ENUMERAÇÃO,
# injetado de fora (a Athena NÃO enumera aqui). Assim o falso-pronto (10/18) nunca passa.
@dataclass(frozen=True)
class ProgressoNotion:
    course_url: str
    no_notion: int
    ts: float

    def completo(self, total) -> bool:
        """Completo SÓ quando o Notion PROVA: no_notion >= total, com total>0. total<=0
        (enumeração vazia/desconhecida) nunca prova conclusão — fail-closed, é o modo de
        falha que gera o falso-pronto."""
        return int(total) > 0 and self.no_notion >= int(total)


def progresso_curso_no_notion(acesso, alvo_container, course_url, *, agora=None
                              ) -> ProgressoNotion:
    """Contagem REAL de aulas deste curso já no Notion (por prefixo de 'Origem'),
    lida DE DENTRO do container do app — mesmo mecanismo do guard anti-duplicidade
    (`curso_ja_no_notion`), só com a sentinela PROGRESSO_NOTION. Devolve um
    ProgressoNotion carimbado; o `total` esperado é aplicado depois em `.completo`.
    LEVANTA se não der para determinar (o chamador escala honesto — nunca assume 0)."""
    qtd = _contar_no_notion(acesso, alvo_container, course_url,
                            sentinela=PROGRESSO_SENTINELA, script=_PROGRESSO_SCRIPT,
                            ctx="[athena progresso] ")
    return ProgressoNotion(course_url=course_url, no_notion=qtd,
                           ts=time.time() if agora is None else agora)


def progresso_fn_observador(acesso, alvo_container, totais_por_curso, agora):
    """Liga a Camada 1 (observador) à contagem REAL do Notion: devolve o seam
    `progresso_fn` que `observador.coletar_estado` chama, produzindo um ProgressoCurso
    por curso com done=contagem no Notion e total=esperado (da enumeração, injetado em
    `totais_por_curso={course_url: total}`). É AQUI que `EstadoObservado.progresso`
    passa a refletir a VERDADE, não um flag — a Camada 1 continua sem falar com o
    Notion (a fonte é plugada por fora, como o observador exige)."""
    def _fn():
        return observador.progresso_captura(
            lambda url: (progresso_curso_no_notion(acesso, alvo_container, url,
                                                   agora=agora).no_notion,
                         totais_por_curso[url]),
            list(totais_por_curso.keys()), agora)
    return _fn


class FilaExecutor:
    """Executor residencial PADRÃO: ENFILEIRA a captura em fila_captura via exec_sql.

    Enfileirar é VPS-safe (é só um INSERT no Postgres, não abre Chrome). Quem executa
    o browser é o worker RESIDENCIAL no Mac, que reivindica a fila — respeitando o
    inviolável de que a captura por browser nunca roda no datacenter. Executores
    alternativos (ex.: callable Mac-side por outro canal) são injetáveis do mesmo jeito;
    o contrato é: `disparar(course_url) -> confirmação truthy`, ou LEVANTA em falha.
    """
    def __init__(self, acesso, projeto):
        self._acesso = acesso
        self._projeto = projeto

    def disparar(self, curso_url):
        # Contrato REAL da fila (painel/fila.py::enfileirar): enfileira por
        # course_url, SEM course_id (nullable — o worker o resolve via vincular_curso
        # após reivindicar). ON CONFLICT casa o índice único PARCIAL real (só sobre
        # course_url WHERE status IN ('enfileirado','capturando')) -> DO NOTHING:
        # re-disparar um curso já ATIVO na fila não duplica (idempotente por URL). Um
        # curso já concluído (pronto/falhou) PODE ser re-enfileirado depois.
        sql = ("INSERT INTO fila_captura (course_url, status) "
               f"VALUES ({_quote(curso_url)}, 'enfileirado') "
               "ON CONFLICT (course_url) WHERE status IN ('enfileirado', 'capturando') "
               "DO NOTHING")
        self._acesso.exec_sql(self._projeto.db_container, sql,
                              db=self._projeto.db_name, user=self._projeto.db_user,
                              rows=False)
        return f"enfileirado:{curso_url}"

    # SEAM DE EVOLUÇÃO (roadmap, NÃO construir agora): hoje o browser roda no worker
    # RESIDENCIAL do Mac (IP residencial), que só liga quando o Mac está ligado. Para
    # captura 24/7 com o Mac desligado, trocar o executor por um que roteie o browser
    # por um PROXY residencial a partir da VPS — mantendo o inviolável anti-ban (IP
    # residencial), sem depender do Mac. É só outro executor injetado: o coordenador
    # não muda.


def coordenar(projeto, acesso, voz, *, executor, curso_url, estado, agora=None,
              ja_no_notion=None, progresso_notion_fn=None):
    """Coordena o PROTOCOLO DE CAPTURA de UM curso (identificado por `curso_url`), um
    passo por ciclo, avançando a máquina de estados em `estado` (dict por curso,
    mutável, persiste entre ciclos). Dirige a `voz` diretamente (pede reseed / avisa /
    escala honesto) e devolve a Acao do passo (ou None quando não há nada a reportar).
    O loop apenas registra a Acao — não re-dirige a voz (o coordenador é dono do seu
    reporte).

    INVIOLÁVEIS cravados aqui:
      - sessão morta -> NÃO dispara captura; pede reseed (nunca loga sozinho);
      - captura SEMPRE via `executor` residencial (nunca Chrome na VPS — anti-ban);
      - nenhuma etapa é dada como sucesso sem confirmação (disparo, ingest);
      - ANTI-DUPLICIDADE: nunca enfileira um curso que o Notion (fonte da verdade
        durável) já mostra capturado — resiliente a restart (checa a cada ciclo);
      - COMPLETUDE POR NOTION, NÃO POR FLAG (gate I-1): quando o tracker diz 'done',
        a conclusão só é DECLARADA se o Auditor CONFIRMAR contra o Notion; falso-pronto
        é REJEITADO e escalado, e o curso NÃO é marcado CONCLUIDO (nem ingerido).

    `ja_no_notion`: seam injetável (curso_url)->(tem_aulas, qtd). Default = checar o
    Notion real via `curso_ja_no_notion` (exec_app no container do app). Injetável
    para os testes passarem um dublê sem tocar Notion/docker.

    `progresso_notion_fn`: seam injetável (curso_url)->ProgressoNotion — a contagem-
    verdade do Notion para o gate de conclusão (I-1). None => gate DESLIGADO (conclui
    pelo tracker como antes; retrocompatível). Em produção o loop liga o seam real
    (`progresso_curso_no_notion`) para que 'concluído' seja SEMPRE provado no Notion.
    """
    agora = time.time() if agora is None else agora
    if ja_no_notion is None:
        ja_no_notion = lambda u: curso_ja_no_notion(projeto, acesso, u)
    fase = estado.get("fase", FASE_NOVO)

    if fase == FASE_CONCLUIDO:
        return None                                       # idempotente: nada a fazer

    # ---- FASE 0: GUARD ANTI-DUPLICIDADE (dono: Athena) ---------------------
    # ANTES de qualquer disparo/sessão, re-checa a FONTE DA VERDADE (Notion). Roda a
    # CADA ciclo: após um Mac-off / fim de sessão / restart da Athena, o estado
    # in-process se perde e a fase volta a NOVO — sem este guard, um curso JÁ
    # capturado seria re-enfileirado (o exato prejuízo). Como a decisão deriva do
    # Notion (durável), e não de memória local, ela sobrevive ao restart (stateless).
    if fase == FASE_NOVO:
        try:
            tem_aulas, qtd = ja_no_notion(curso_url)
        except Exception as e:
            # Não deu para confirmar: NÃO enfileira às cegas (evita re-captura) e
            # ESCALA honesto; a fase segue NOVO -> o próximo ciclo re-tenta.
            pedido = (f"[{projeto.nome}] NÃO consegui verificar no Notion se {curso_url} "
                      f"já foi capturado (anti-duplicidade): {str(e)[:140]} — NÃO "
                      f"enfileiro (evito re-captura às cegas); re-tento no próximo ciclo")
            voz.escalar(Problema("antidup_notion_inacessivel", curso_url, pedido, "aviso"),
                        pedido)
            return Acao("", False, True, pedido)
        if tem_aulas:
            # JÁ capturado: pula o enfileiramento. Marca CONCLUIDO só para não
            # re-reportar a cada ciclo DESTE processo — a correção NÃO depende disso:
            # numa instância nova (pós-restart) a fase volta a NOVO e o Notion re-decide
            # o skip do zero.
            estado["fase"] = FASE_CONCLUIDO
            acao = Acao(f"[{projeto.nome}] {curso_url} JÁ capturado ({qtd} aulas no Notion) "
                        f"— pulo o enfileiramento (anti-duplicidade)", True, False)
            voz.avisar_acao(acao)
            return acao

    # ---- FASE 1: checar a sessão ANTES de qualquer disparo -----------------
    if fase == FASE_NOVO:
        sess = estado_sessao(projeto, acesso, curso_url)
        if sess == "morta":
            # INVIOLÁVEL: nunca loga sozinho. PEDE reseed humano via voz (Telegram).
            pedido = (f"[{projeto.nome}] sessão da plataforma MORTA (sinal '{STATUS_SESSAO_MORTA}' "
                      f"na fila) — preciso de RESEED (re-semear o storage_state pelo Mac e me "
                      f"avisar); NÃO faço login sozinho e NÃO capturo {curso_url} sem sessão viva")
            voz.escalar(Problema("sessao_morta", curso_url, pedido, "critico"), pedido)
            return Acao("", False, True, pedido)

        # 'desconhecida' NÃO bloqueia: o Maestro segue enfileirando. O worker
        # residencial é o detector real da sessão (storage_state local) no claim; se
        # ela estiver morta, ele publica o sinal e o próximo ciclo pede reseed.
        # ---- FASE 2: dispara via EXECUTOR residencial ----------------------
        # O Maestro roda na VPS (datacenter). Capturar por browser AQUI queimaria a
        # conta (anti-ban). Então NÃO abrimos Chrome: delegamos ao executor, que dispara
        # o worker RESIDENCIAL (Mac). Falha/silêncio do executor -> escala honesto.
        try:
            confirmacao = executor.disparar(curso_url)
        except Exception as e:
            pedido = (f"[{projeto.nome}] FALHEI ao disparar a captura residencial de "
                      f"{curso_url}: {str(e)[:160]}")
            voz.escalar(Problema("captura_disparo_falhou", curso_url, pedido, "critico"), pedido)
            return Acao("", False, True, pedido)
        if not confirmacao:
            pedido = (f"[{projeto.nome}] disparo da captura de {curso_url} SEM confirmação "
                      f"do executor residencial — não assumo sucesso")
            voz.escalar(Problema("captura_disparo_sem_confirmacao", curso_url, pedido, "critico"),
                        pedido)
            return Acao("", False, True, pedido)
        estado["fase"] = FASE_CAPTURANDO
        acao = Acao(f"[{projeto.nome}] captura de {curso_url} disparada no residencial: "
                    f"{confirmacao}", True, False)
        voz.avisar_acao(acao)
        return acao

    # ---- FASE 3: monitorar progresso via Acesso (tracker) ------------------
    if fase == FASE_CAPTURANDO:
        # C4: monitorar exige o course_id, que o worker resolve (vincular_curso) SÓ
        # após reivindicar o job. Resolve-o da fila a cada ciclo (o worker pode ter
        # reivindicado desde a última vez).
        try:
            course_id = resolver_course_id(projeto, acesso, curso_url)
        except Exception as e:
            pedido = (f"[{projeto.nome}] não consigo ler a fila para resolver o course_id "
                      f"de {curso_url}: {str(e)[:140]}")
            voz.escalar(Problema("fila_inacessivel", curso_url, pedido, "aviso"), pedido)
            return Acao("", False, True, pedido)
        if not course_id:
            # AGUARDANDO REIVINDICAÇÃO: o worker ainda não pegou o job (course_id nulo).
            # NÃO é erro nem falha — só espera o próximo ciclo. Nem monitora às cegas
            # (não consulta estado_aulas sem course_id), nem escala.
            return None

        try:
            total, done, pend = progresso(projeto, acesso, course_id)
        except Exception as e:
            pedido = (f"[{projeto.nome}] não consigo ler o progresso do curso {course_id} "
                      f"({curso_url}): {str(e)[:140]}")
            voz.escalar(Problema("progresso_inacessivel", curso_url, pedido, "aviso"), pedido)
            return Acao("", False, True, pedido)
        if not (total > 0 and pend == 0):
            return None                                   # ainda capturando: quieto (não spamma)

        # ---- GATE I-1: COMPLETUDE POR NOTION, NÃO POR FLAG --------------------
        # O tracker (estado_aulas) diz 'done' — mas o tracker é um FLAG local, a exata
        # fonte que já mentiu 'pronto' com aulas pendentes. Antes de declarar concluído
        # (ou até de ingerir), o Auditor CONFIRMA contra a FONTE DE VERDADE (Notion):
        # só passa se no_notion >= total. Falso-pronto é REJEITADO, escalado via voz, e
        # o curso NÃO avança — o próximo ciclo re-tenta quando o Notion alcançar `total`.
        # Seam desligado (None) => comportamento antigo (retrocompat). `total` é o
        # esperado, vindo da ENUMERAÇÃO do tracker (a Athena não re-enumera aqui).
        if progresso_notion_fn is not None:
            try:
                prog_notion = progresso_notion_fn(curso_url)
            except Exception as e:
                pedido = (f"[{projeto.nome}] curso {curso_url} reportado done pelo tracker, "
                          f"mas NÃO consegui confirmar no Notion (gate I-1): {str(e)[:140]} "
                          f"— NÃO declaro concluído; re-tento no próximo ciclo")
                voz.escalar(Problema("conclusao_notion_inacessivel", curso_url, pedido, "aviso"),
                            pedido)
                return Acao("", False, True, pedido)
            from maestro import auditor
            laudo = auditor.auditar_conclusao(curso_url, prog_notion, total, voz=voz)
            if not laudo.aprovado:
                # FALSO-PRONTO: auditar_conclusao já escalou o gap honesto. NÃO ingere,
                # NÃO marca CONCLUIDO — a fase segue CAPTURANDO (re-tenta).
                falta = max(total - prog_notion.no_notion, 0)
                return Acao("", False, True,
                            f"[{projeto.nome}] conclusão de {curso_url} REJEITADA pelo Auditor "
                            f"(Notion tem {prog_notion.no_notion}/{total} — {falta} faltando)")

        # ---- FASE 4: curso completo -> AUTO-INGEST reusando reconciliar ----
        # REUSO (não reimplementa): forço o disparo AGORA passando ultimo bem no
        # passado, furando a cadência periódica de reconciliar de propósito — curso
        # recém-concluído deve ser ingerido já, não no próximo intervalo de 30 min.
        acao, _ = conhecimento.reconciliar(
            projeto, acesso, agora=agora,
            ultimo=agora - conhecimento.INTERVALO_RECONCILE_S - 1)
        if acao is None:
            # sem app_container -> não há alvo de ingest; honesto, não some silencioso.
            pedido = (f"[{projeto.nome}] curso {curso_url} capturado mas SEM app_container "
                      f"para auto-ingest — verificar o registro do projeto")
            voz.escalar(Problema("ingest_sem_alvo", curso_url, pedido, "aviso"), pedido)
            return Acao("", False, True, pedido)
        if not acao.executada:
            # reconcile falhou / sem RECONCILE_OK -> NÃO marca concluído; escala honesto.
            voz.escalar(Problema("ingest_falhou", curso_url, acao.pedido, "aviso"), acao.pedido)
            return acao
        estado["fase"] = FASE_CONCLUIDO
        voz.avisar_acao(acao)
        # SEAM DA ESTEIRA (declarado, não implementado): classificador fino + Sintetizador.
        _hooks_esteira(projeto, curso_url, estado)
        return acao

    return None


def _hooks_esteira(projeto, curso_url, estado) -> list:
    """SEAM da esteira downstream — ponto de extensão APÓS o auto-ingest confirmado.
    Hoje é um NO-OP honesto (não faz nada e não finge que fez): devolve [] ações.

    Aqui entram, quando construídos (fora do escopo agora):
      - CLASSIFICADOR FINO: hoje inline no motor; passará a rodar como etapa própria
        na esteira, classificando o curso/aulas por tipo/intenção.
      - SINTETIZADOR: só para cursos how_to — extrai passo-a-passo/skills/agentes a
        partir das aulas já ingeridas.
    Ambos consomem o que o auto-ingest deixou no pgvector; por isso o gancho é DEPOIS
    da ingestão confirmada, nunca antes.
    """
    return []
