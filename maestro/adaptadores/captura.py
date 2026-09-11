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
import hashlib
import json
import os
import re
import shlex
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from maestro import observador
from maestro.adaptador_pipeline import plataforma_de_url, plataforma_suportada
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


# ============================================================================
# EXECUTOR DOMÉSTICO — a Athena rodando NO MAC, chamando o MOTOR DIRETO.
# ============================================================================
# Decisão de arquitetura (INVIOLÁVEL): a Athena doméstica NÃO enfileira no painel-api
# para um worker da VPS reivindicar (isso é o MODELO VPS). No Mac, ELA MESMA é o
# residencial: dispara `python -m motor.cli|motor.memberkit <curso>` como SUBPROCESSO
# LOCAL. O contrato do executor é o MESMO do FilaExecutor (`disparar(url) -> confirmação
# truthy | LEVANTA`), então o `coordenar`/`orquestrar_captura` não sabem a diferença —
# só se TROCA o executor (fila -> local), como manda a tarefa.
class ContaOcupada(RuntimeError):
    """Levantada quando se tenta disparar uma 2ª captura na MESMA conta enquanto outra
    ainda roda. NÃO é falha de infra — é o guard ANTI-BAN inviolável FUNCIONANDO
    ('nunca 2 capturas na mesma conta ao mesmo tempo'). O chamador doméstico a trata
    como 'aguarda a vez' (fail-safe), nunca como sucesso nem como crash."""


class MaquinaSobrecarregada(ContaOcupada):
    """Disparo ADIADO pelo PORTÃO DE CARGA (`maestro.carga`): a máquina está sobrecarregada
    (carga média / memória livre) ou no teto de motores simultâneos. Levantada por
    `LocalExecutor.disparar` DEPOIS do guard anti-ban e ANTES de qualquer lock/spawn.

    NÃO é falha (incidente 10/09: disparar num Mac sufocado só fabricava mortes): não conta
    disjuntor, flap nem tentativa. Herda de `ContaOcupada` de propósito — quem só conhece o
    "aguarda a vez" já a trata como espera benigna (fail-safe); o loop doméstico a captura
    ANTES para registrar a decisão e o aviso agregado. `motivo` = o texto do portão.
    `escalonamento` False = sobrecarga de verdade (conta no episódio do aviso agregado)."""
    escalonamento = False

    def __init__(self, motivo):
        super().__init__(motivo)
        self.motivo = motivo


class DisparoEscalonado(MaquinaSobrecarregada):
    """Disparo ADIADO pela RAMPA da saída da sobrecarga (`maestro.carga`): a máquina já
    aliviou, mas o teto de disparos novos por ciclo (ATHENA_CARGA_DISPAROS_POR_CICLO) foi
    atingido — o sensor ainda não reflete os motores recém-lançados. Mesmo tratamento do
    adiamento (não é falha), mas NÃO prolonga o episódio de sobrecarga do aviso."""
    escalonamento = True


class AguardaAutopsia(ContaOcupada):
    """Disparo RECUSADO porque a CONTA tem um óbito colhido (`_reap`) e ainda NÃO drenado
    pela autópsia do ciclo (achado r6: a espera só existia na passada; um motor que morria
    ENTRE o `_aguardando_autopsia` da passada e o `_reap` do `disparar` — a janela inclui a
    leitura de pendência do tracker e o disjuntor — deixava outro curso da MESMA conta
    disparar antes da causa, inclusive sobre uma sessão morta). `disparar` é o ponto único
    por onde todo disparo passa; o `drenar_obitos` do próximo ciclo libera. Herda de
    `ContaOcupada`: quem só conhece o "aguarda a vez" já trata como espera (não é falha)."""


@dataclass(frozen=True)
class CursoLocal:
    """Um curso desejado no modelo DOMÉSTICO. `conta` é a chave de serialização
    anti-ban (contas diferentes rodam em paralelo; a MESMA conta, nunca). `plataforma`
    escolhe o módulo do motor e a política headed/headless. `total_esperado` é o
    DENOMINADOR (opcional) da completude-por-Notion — 0 = desconhecido => o owner
    NUNCA declara concluído (fail-closed, anti-falso-pronto). `session_path` (opcional)
    aponta o storage_state EXISTENTE da conta (semeado por login manual — NUNCA
    re-logamos): quando a plataforma declara um `session_env` no spec, `_montar` injeta
    este caminho no env do motor; vazio => o motor usa o default dele (cwd=motor_dir).
    `tenant` (opcional) é o identificador do TENANT em plataformas white-label
    (Curseduca: uuid do tenant, exigido pelo motor p/ resolver vídeo no player — NÃO é
    segredo): quando o spec declara `tenant_env`, `_montar` o injeta DEPOIS do
    `spec.env`, logo um tenant por-curso VENCE o default fixado no spec; vazio => vale
    o default do spec (o caso de hoje: tenant único segueadi)."""
    url: str
    conta: str
    plataforma: str = "hotmart"
    total_esperado: int = 0
    session_path: str = ""
    tenant: str = ""


@dataclass(frozen=True)
class PlataformaSpec:
    """Como o LocalExecutor invoca o MOTOR de UMA plataforma. Um registro por plataforma
    suportada — fora do mapa `_PLATAFORMAS` => fail-closed (o gate de plataforma-nova já
    deveria ter barrado; isto é a última linha de defesa).

    Campos que CRAVAM os invioláveis anti-ban por plataforma:
      - `modulo`: o módulo do motor (`python -m <modulo> <url>`).
      - `passes`: a tupla de PASSES de trabalho que o motor desta plataforma sabe rodar,
        em ORDEM DE ANEL. Cada disparo escolhe UM passe (ver `LocalExecutor._escolher_passe`)
        e `_montar` anexa a flag correspondente (`_PASSE_FLAG`): base=sem flag,
        audio=`--audio`, embed=`--embed`, nao-video=`--nao-video`. Hotmart (motor.cli)
        tem os 4; a STOA (motor.stoa, com as 3 ferramentas) tem `("base","embed",
        "nao-video")` — o `base` dela JÁ é o áudio-nativo (modo default do CLI), então
        não existe passe `audio` separado. Os demais adaptadores áudio-nativos
        (memberkit/kajabi) ficam em `("base",)` — NÃO conhecem flag nenhuma (passá-la
        seria argumento desconhecido), então para eles a escolha de passe é um no-op
        (sempre "base", NADA muda no comando).
      - `headless`: True => HEADLESS=1 (Memberkit, áudio-nativo, roda cego); False =>
        HEADED (Hotmart/Stoa/Kajabi — a sonda de sessão e/ou a captura de áudio do
        player de vídeo falham headless).
      - `chromium`: True => MOTOR_BROWSER=chromium (o Chromium EMBUTIDO, binário SEPARADO
        que NÃO colide com o Google Chrome do sistema no ProcessSingleton do macOS); False
        => channel=chrome (Hotmart/Memberkit, retrocompat do spike). É o que deixa Stoa/
        Kajabi rodarem em PARALELO ao Hotmart sem brigar pela única instância de Chrome do
        SO. (O executor NÃO dirige navegador — só escolhe a env; quem lança é o motor.)
      - `url_env`: nome do env que recebe a `meta.url` (STOA_URL/KAJABI_URL). Redundante
        com o argv (o CLI resolve argv-first), mas espelha o launcher manual e cobre
        qualquer caminho do motor que leia o env em vez do argv. '' => não seta.
      - `env`: env FIXO extra da plataforma (perfil Chrome DEDICADO por conta via
        CHROME_USER_DATA_DIR; LESSON_TIMEOUT_S). Aplicado DEPOIS do extra_env global (a
        config da plataforma vence) e ANTES dos invioláveis (Groq/HEADLESS/MOTOR_BROWSER
        vencem tudo).
      - `session_env`: nome do env que recebe o `CursoLocal.session_path` (o
        storage_state EXISTENTE, semeado por login manual). '' => não injeta nada e o
        motor usa o default dele relativo ao cwd — é o caso das plataformas já
        integradas (hotmart/memberkit/stoa/kajabi), mantidas INTACTAS de propósito.
      - `tenant_env`: nome do env que recebe o `CursoLocal.tenant` (identificador do
        TENANT em plataformas white-label — Curseduca: CURSEDUCA_TENANT_UUID). Injetado
        DEPOIS do `spec.env`, então o tenant POR-CURSO (YAML) vence o default fixado no
        spec — é o caminho multi-tenant sem tocar código. '' => não injeta nada
        (todas as plataformas exceto curseduca; aditivo, vivas intactas).
      - `sessao_por_tenant`: True => a sessão é CREDENCIAL DE UM TENANT e o daemon
        NUNCA deixa o motor cair na de outro (inviolável "nunca duas contas/tenants
        misturando credencial"). `_montar` recusa (fail-closed, sem spawn): curso SEM
        `session_path` (o default do motor pode ser um arquivo único para todos os
        tenants — Entrega Digital: `.entregadigital-session.json`) e `session_path`
        que outro curso do YAML usa com OUTRA conta ou OUTRO host. False (as vivas) =>
        nada muda.
      - `hosts`: hosts (exatos ou sufixo, a mesma regra do gate de domínio) que SÓ esta
        plataforma captura. Um curso num desses hosts com OUTRA `plataforma` no YAML
        (ex.: a linha `plataforma:` esquecida => o carregador assume "hotmart" e
        rodaria o motor Hotmart no perfil `.chrome-profile` VIVO) é recusado em
        `_montar`. É checagem cruzada, não gate: host fora da lista não é barrado
        aqui (quem barra é `PLATAFORMAS_SUPORTADAS`). '' (as vivas) => nada muda."""
    modulo: str
    passes: tuple = ("base",)
    headless: bool = False
    chromium: bool = False
    url_env: str = ""
    env: tuple = ()
    session_env: str = ""
    tenant_env: str = ""
    sessao_por_tenant: bool = False
    hosts: tuple = ()


# plataforma -> como invocar o motor. Fora deste mapa => fail-closed (o motor só sabe
# estas; capturar numa plataforma desconhecida às cegas violaria o anti-ban/o gate de
# plataforma-nova).
#   - hotmart:  CLI base, RODÍZIO dos 4 passes (base/embed/audio/nao-video), HEADED,
#     channel=chrome. O passe é escolhido POR DEMANDA a cada disparo (o que tiver
#     pendência no tracker), 1 passe por disparo — nunca 2 na mesma conta (anti-ban).
#   - memberkit: adaptador próprio, áudio-nativo, HEADLESS, channel=chrome — só `base`.
#   - stoa: adaptador próprio, HEADED, Chromium ISOLADO — RODÍZIO de 3 passes
#     (base=áudio-nativo / embed=vídeo cross-plataforma / nao-video=doc→Notion), os 2
#     novos GATED por ATHENA_STOA_PASSES_ATIVO (ver `_passe_ativavel`). Continua 1 motor
#     por conta (o rodízio muda só o argv do único spawn — anti-ban intacto).
#   - kajabi: adaptador próprio, áudio-nativo, HEADED, Chromium ISOLADO (perfil
#     dedicado por conta) — nunca channel=chrome (colidiria com o Hotmart no singleton).
_PLATAFORMAS = {
    # Hotmart FIXA seu perfil histórico `.chrome-profile` (o MESMO do launcher manual que
    # PRODUZ): a sessão Hotmart restaura cookies DO PRÓPRIO PERFIL ("restaurando N cookie(s)
    # do perfil") além do storage_state, então um perfil NOVO/vazio arriscaria a sessão. Não
    # colide: é conta ÚNICA (hotmart-principal) e os 3 Memberkit (que partilhavam este mesmo
    # default) agora têm perfis DEDICADOS — Hotmart fica sozinho no `.chrome-profile`.
    # + passe "youtube" (braço YouTube do motor, deploy 3517c47): `motor.cli --youtube`
    # re-seleciona `video_youtube`/`transcrevendo_youtube` e RESGATA as `nao_video_erro`
    # por YouTube via `requeue_youtube_from_nao_video_erro` (as 57 do ciro-gestor).
    # GATED por ATHENA_YOUTUBE_ATIVO (`_passe_ativavel`); HERDA a política do spec
    # (HEADED, channel=chrome, mesmo perfil) — o rodízio muda só o argv do único spawn.
    "hotmart": PlataformaSpec(
        "motor.cli", passes=("base", "embed", "audio", "nao-video", "youtube"),
        env=(("CHROME_USER_DATA_DIR", ".chrome-profile"),)),
    # Memberkit: 3 tenants (contas distintas) — SEM perfil no spec => cada conta ganha o seu
    # (`.chrome-profile-<conta>`) em `_montar`. É o fix do flap: os 3 + Hotmart caíam todos
    # no `.chrome-profile` e colidiam no ProcessSingleton. Sessão é storage_state (provado:
    # os 3 injetam cookies e enumeram o próprio tenant), então perfil dedicado não perde login.
    # `session_env`: cada tenant tem seu PRÓPRIO storage_state (session_path no YAML) — o motor
    # (motor.memberkit) lê UM único MEMBERKIT_SESSION_PATH do env (default .memberkit-session.json).
    # SEM esta injeção, os 3 tenants caíam TODOS no mesmo default e disputavam UMA sessão (só o
    # que casasse o .env capturava; os outros falhavam a sonda e escalavam reseed). Com o
    # session_env, `_montar` injeta o session_path POR-CURSO => cada tenant usa a SUA sessão.
    "memberkit": PlataformaSpec(
        "motor.memberkit", headless=True, session_env="MEMBERKIT_SESSION_PATH"),
    "stoa": PlataformaSpec(
        "motor.stoa", passes=("base", "embed", "nao-video"),
        chromium=True, url_env="STOA_URL",
        env=(("CHROME_USER_DATA_DIR", ".chrome-profile-stoa"),
             ("LESSON_TIMEOUT_S", "1800"))),
    "kajabi": PlataformaSpec(
        "motor.kajabi", chromium=True, url_env="KAJABI_URL",
        env=(("CHROME_USER_DATA_DIR", ".chrome-profile-kajabi"),)),
    # ---- 5 ADAPTADORES NOVOS (sessões semeadas 20/07 por login manual) ----------
    # Todos: passe ÚNICO ("base" — os CLIs não conhecem --audio/--embed; a transcrição
    # é áudio-nativa via Groq dentro do próprio pipeline), HEADLESS (no motor,
    # HEADLESS=1 => `allow_reseed=False` => sessão morta ABORTA com SessionDeadError e
    # a autópsia escala reseed — fail-closed, NUNCA login automático), channel=chrome
    # (chromium=False: o MESMO canal que SEMEOU as sessões; trocar de engine mudaria o
    # fingerprint) e perfil DEDICADO por conta via `_perfil_de_conta` (sem
    # CHROME_USER_DATA_DIR no spec) — nunca colidem no ProcessSingleton. `session_env`
    # aponta o storage_state EXISTENTE (CursoLocal.session_path do YAML); `url_env`
    # espelha o argv (argv-first no CLI), como Stoa/Kajabi.
    "kiwify": PlataformaSpec(
        "motor.kiwify", headless=True, url_env="KIWIFY_URL",
        session_env="KIWIFY_SESSION_PATH"),
    "nutror": PlataformaSpec(
        "motor.nutror", headless=True, url_env="NUTROR_URL",
        session_env="NUTROR_SESSION_PATH"),
    "alpaclass": PlataformaSpec(
        "motor.alpaclass", headless=True, url_env="ALPACLASS_URL",
        session_env="ALPACLASS_SESSION_PATH"),
    "hubla": PlataformaSpec(
        "motor.hubla", headless=True, url_env="HUBLA_URL",
        session_env="HUBLA_SESSION_PATH"),
    "greenn": PlataformaSpec(
        "motor.greenn", headless=True, url_env="GREENN_URL",
        session_env="GREENN_SESSION_PATH"),
    # ---- CURSEDUCA (white-label; tenant segueadi) — PROVADO AO VIVO 24/07 -------
    # Smoke real: `python -m motor.curseduca --limit 2 --course 164` HEADLESS
    # capturou 2/2 aulas (download HLS Bunny funciona headless) → páginas REAIS no
    # Notion. Mesmo padrão dos 5 novos: passe ÚNICO ("base" — o CLI não tem flags
    # de passe; enumeração + vídeo + Whisper/Groq + Notion num run só), HEADLESS
    # (allow_reseed=False => sessão morta ABORTA fail-closed, nunca login
    # automático — e o usuário dormindo não vê janela), channel=chrome (o MESMO
    # canal que semeou a sessão), perfil DEDICADO por conta via `_perfil_de_conta`
    # (sem CHROME_USER_DATA_DIR no spec — nunca o `.chrome-profile` do Hotmart).
    # TENANT (CURSEDUCA_TENANT_UUID — identificador, não segredo; o motor o exige
    # p/ resolver o vídeo no player): é POR-TENANT. Hoje há UM tenant Curseduca
    # (segueadi), fixado AQUI como default. ATENÇÃO multi-tenant futuro: um 2º
    # tenant Curseduca EXIGE tenant por-curso — já plumbado via `CursoLocal.tenant`
    # (YAML `tenant:`) → `tenant_env`, injetado DEPOIS deste env => VENCE o
    # default. Basta a entrada YAML; não toque neste spec.
    "curseduca": PlataformaSpec(
        "motor.curseduca", headless=True, url_env="CURSEDUCA_URL",
        session_env="CURSEDUCA_SESSION_PATH", tenant_env="CURSEDUCA_TENANT_UUID",
        env=(("CURSEDUCA_TENANT_UUID", "4037b710-50c5-11ed-b97b-16058182e383"),)),
    # ---- CADEMÍ e ENTREGA DIGITAL (rodada 9) — REGISTRADAS, DESLIGADAS ------------
    # Nada dispara sem uma entrada no YAML E o host no gate de domínio (a env
    # PLATAFORMAS_SUPORTADAS do launch.sh vivo é explícita). Mesmo padrão dos adaptadores
    # de 22/07: passe ÚNICO ("base" — os CLIs não têm flag de passe), HEADLESS (no motor:
    # `allow_reseed=False` => sessão morta sai 3, nunca login automático), channel=chrome,
    # perfil DEDICADO por conta via `_perfil_de_conta` (sem CHROME_USER_DATA_DIR aqui — o
    # default do CLI da Entrega Digital é o `.chrome-profile` do Hotmart VIVO; o daemon
    # sempre força o da conta). `url_env`/`session_env` = os nomes que os CLIs leem
    # (motor/cademi/cli.py: CADEMI_URL, CADEMI_SESSION_PATH; motor/entregadigital/cli.py:
    # ENTREGADIGITAL_URL, ENTREGADIGITAL_SESSION_PATH). `sessao_por_tenant`: a sessão do
    # YAML é obrigatória e exclusiva do (conta, host). `hosts`: a checagem cruzada
    # host -> plataforma. Cademí: os 3 tenants de domínio próprio NÃO têm sufixo comum
    # (a plataforma vem do `plataforma:` do YAML; aqui só o cruzamento dos conhecidos).
    # Tracker: `pendencia_capturavel_local(..., plataforma=)` lê os course_id
    # `cademi:<host>:<id>` / `entregadigital:<tenant>:product:<pid>` do tenant inteiro.
    "cademi": PlataformaSpec(
        "motor.cademi", headless=True, url_env="CADEMI_URL",
        session_env="CADEMI_SESSION_PATH", sessao_por_tenant=True,
        hosts=("membros.alfaresearch.com.br", "cursos.codigoviral.com.br",
               "aulas.ramonpereira.com.br")),
    "entregadigital": PlataformaSpec(
        "motor.entregadigital", headless=True, url_env="ENTREGADIGITAL_URL",
        session_env="ENTREGADIGITAL_SESSION_PATH", sessao_por_tenant=True,
        hosts=("entregadigital.app.br",)),
}


# Chave PRIVADA de env pela qual o executor diz ao spawn default ONDE tee'ar o stderr do
# filho. É POPADA dentro do `_spawn_popen` ANTES do exec — o motor NUNCA a herda (não é
# config dele; é fiação interna do supervisor). O seam de spawn injetado (testes) ignora
# a chave, então o contrato `(cmd, *, env, cwd)` fica intacto.
_ENV_STDERR_TEE = "_ATHENA_MOTOR_STDERR"


# PASSE -> flag de CLI do motor. Consumido pelo Hotmart (motor.cli, os 5) E pela Stoa
# (motor.stoa: base/--embed/--nao-video). `base` = passe default SEM flag (legenda/vídeo
# nativo no Hotmart; áudio-nativo na Stoa). Os demais ligam os seletores próprios do
# tracker de cada motor (audio/embed/nao_video/youtube_pending; pools Stoa:
# _PENDING_POR_MODO). `youtube` é EXCLUSIVO do Hotmart (só motor.cli conhece --youtube).
_PASSE_FLAG = {"base": None, "audio": "--audio", "embed": "--embed",
               "nao-video": "--nao-video", "youtube": "--youtube"}

# ESPELHO de motor/tracker.py::PENDING_EXCLUDED — a FONTE-VERDADE vive lá; o executor NÃO
# pode importar o pacote `motor` (árvore/pkg separados, roda via `python -m` noutro cwd).
# Se `PENDING_EXCLUDED` mudar em tracker.py, ESTA lista tem que mudar junto (contrato).
# `base` conta as aulas que o passe de legenda/vídeo nativo pega = tudo que NÃO está aqui.
_PENDING_EXCLUDED = (
    "no_notion", "sem_legenda", "sem_video", "transcrevendo", "audio_erro",
    "sem_audio", "transcrevendo_embed", "sem_embed", "capturando_nao_video",
    "sem_conteudo",
    # espelho do tracker pós-braço-YouTube (deploy 3517c47): nao_video_erro é terminal
    # do braço de NÃO-VÍDEO (só o resgate --youtube o toca); video_youtube/
    # transcrevendo_youtube pertencem EXCLUSIVAMENTE ao passe --youtube.
    "nao_video_erro", "video_youtube", "transcrevendo_youtube",
)
# Espelho de AUDIO_PENDING / EMBED_PENDING / NAO_VIDEO_PENDING / YOUTUBE_PENDING
# (tracker.py). Idem contrato.
_STATUSES_POR_PASSE = {
    "audio": ("sem_legenda", "transcrevendo"),
    "embed": ("sem_video", "transcrevendo_embed"),
    "nao-video": ("sem_embed", "capturando_nao_video"),
    "youtube": ("video_youtube", "transcrevendo_youtube"),
}

# ESPELHO do critério de RESGATE de `requeue_youtube_from_nao_video_erro` (tracker.py):
# `nao_video_erro` com a assinatura LEGADA ('provedor não suportado' + 'youtube') é
# trabalho REAL do passe --youtube (o requeue roda DENTRO do passe), mas essas aulas só
# viram `video_youtube` DEPOIS que o passe roda uma vez — sem contá-las aqui o rodízio
# nunca escolheria o passe e o resgate jamais aconteceria (deadlock). Exigir 'provedor
# não suportado' (e não '%youtube%' solto) espelha o motor: o terminal defensivo
# 'nenhum vídeo YouTube capturável' NÃO conta (senão o rodízio dispararia --youtube à
# toa para sempre). Se o critério mudar em tracker.py, ESTE SQL muda junto (contrato).
_SQL_YOUTUBE_RESGATE = (
    "SELECT COUNT(*) FROM lessons WHERE course_id=? AND status='nao_video_erro' "
    "AND error LIKE '%provedor não suportado%' AND error LIKE '%youtube%'")

# ESPELHO de motor/stoa/pipeline.py::_PENDING_POR_MODO (a FONTE-VERDADE vive lá; mesmo
# contrato de espelho do Hotmart acima). Na Stoa os pools DIFEREM do Hotmart:
#   - `base` é o áudio-nativo (modo default do CLI): pendente + transcrevendo (resume);
#   - `embed` re-visita as `sem_audio` (o braço de áudio não achou player Hotscool —
#     pode ser embed externo OU doc/texto; o passe --embed é quem separa) — é o pool
#     que torna as 178 sem_audio históricas re-selecionáveis;
#   - `nao-video` pega as `sem_embed` (confirmadas doc/texto pelo --embed) + resume.
# Se `_PENDING_POR_MODO` mudar no pipeline da Stoa, ESTE mapa muda junto (contrato).
_STOA_STATUSES_POR_PASSE = {
    "base": ("pendente", "transcrevendo"),
    "embed": ("sem_audio", "transcrevendo_embed"),
    "nao-video": ("sem_embed", "capturando_nao_video"),
}


def _course_id_de_url(curso_url):
    """product-id Hotmart do trecho `/products/<id>` da URL. O tracker chaveia por
    course_id (NÃO por url: a coluna `courses.url` é não-confiável — dezenas de course_id
    compartilham uma url stale), e o product-id casa 1:1 com o course_id nas contas
    Hotmart. None se a URL não tiver esse trecho."""
    if not curso_url:
        return None
    achados = re.findall(r"/products/([^/?#]+)", curso_url)
    return achados[-1] if achados else None


def _pendencias_tracker(curso_url, motor_dir):
    """Lê `{motor_dir}/tracker.db` em SQLite READ-ONLY e devolve {passe: qtd_pendente}
    para o curso (base/audio/embed/nao-video). NÃO muta e NÃO trava o motor vivo (uri
    `mode=ro` + `busy_timeout`; o WAL do motor permite leitura concorrente). Resolve o
    course_id pelo product-id da URL (ver `_course_id_de_url`). Devolve **None** em
    QUALQUER erro (db ausente, course_id não resolve, SQL) — o chamador então cai no
    round-robin CEGO (fail-open p/ RODÍZIO, jamais p/ paralelismo)."""
    course_id = _course_id_de_url(curso_url)
    if not course_id:
        return None
    db_path = os.path.join(motor_dir, "tracker.db")
    if not os.path.exists(db_path):
        return None
    con = None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        con.execute("PRAGMA busy_timeout=5000")
        linhas = con.execute(
            "SELECT status, COUNT(*) FROM lessons WHERE course_id=? GROUP BY status",
            (course_id,),
        ).fetchall()
        # RESGATE YouTube: as `nao_video_erro` legadas por YouTube são trabalho do passe
        # --youtube (o requeue do motor as move ao rodar) — contam no pool p/ o rodízio
        # ESCOLHER o passe (ver o comentário de _SQL_YOUTUBE_RESGATE).
        resgate_youtube = con.execute(
            _SQL_YOUTUBE_RESGATE, (course_id,)).fetchone()[0]
    except sqlite3.Error:
        return None
    finally:
        if con is not None:
            con.close()
    por_status = {s: n for (s, n) in linhas}
    pend = {p: 0 for p in _PASSE_FLAG}
    for status, n in por_status.items():
        if status not in _PENDING_EXCLUDED:
            pend["base"] += n
    for passe, statuses in _STATUSES_POR_PASSE.items():
        pend[passe] = sum(por_status.get(s, 0) for s in statuses)
    pend["youtube"] += resgate_youtube
    return pend


def _pendencias_tracker_stoa(curso_url, motor_dir):
    """Leitor de pendências da STOA: lê `{motor_dir}/tracker.db` (o do worktree
    adaptador-stoa, via ATHENA_MOTOR_DIR_STOA) em SQLite READ-ONLY e devolve
    {passe: qtd_pendente} pelos pools de `_STOA_STATUSES_POR_PASSE`.

    DIFERE do leitor Hotmart em DOIS pontos, ambos de propósito:
      - SEM course_id específico (a Stoa é curso-único no daemon — 1 CursoLocal
        "meus-cursos", conta stoa-principal — e a URL não tem `/products/<id>`; o
        run do motor enumera o tenant INTEIRO, 18 course_ids, então agregar todos
        é exatamente o trabalho que o próximo disparo verá), MAS filtrando
        `course_id LIKE 'stoa:%'` — o prefixo é CONTRATO do motor
        (motor/stoa/enumerate.py: `stoa:<subdominio>:<id>`, "impede colisão com
        Hotmart/Memberkit"). Defesa em profundidade (achado do review): se
        ATHENA_MOTOR_DIR_STOA sumir do env, `_motor_dir_de` cai CALADO no
        motor_dir genérico (/aula, o do Hotmart), cujo tracker tem
        sem_audio/sem_embed de outras plataformas — sem o filtro, o rodízio da
        Stoa decidiria passes com pendências ALHEIAS, plausível e errado. Com o
        filtro, esse fallback rende contagens 0 => sonda `base` (benigno);
      - pools próprios (`sem_audio` no papel de candidata a embed, ver o espelho).
    Devolve None em QUALQUER erro (db ausente/SQL) — rodízio CEGO (fail-open p/
    RODÍZIO, jamais p/ paralelismo; o anti-ban não passa por aqui)."""
    db_path = os.path.join(motor_dir, "tracker.db")
    if not os.path.exists(db_path):
        return None
    con = None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        con.execute("PRAGMA busy_timeout=5000")
        linhas = con.execute(
            "SELECT status, COUNT(*) FROM lessons "
            "WHERE course_id LIKE 'stoa:%' GROUP BY status").fetchall()
    except sqlite3.Error:
        return None
    finally:
        if con is not None:
            con.close()
    por_status = {s: n for (s, n) in linhas}
    return {passe: sum(por_status.get(s, 0) for s in statuses)
            for passe, statuses in _STOA_STATUSES_POR_PASSE.items()}


# Leitor de pendências DEFAULT por plataforma (só consultado quando nenhum
# `pendencias_fn` foi injetado): a Stoa agrega o tracker do worktree pelos pools
# próprios; as demais multi-passe (Hotmart) usam o leitor por course_id.
_PENDENCIAS_PADRAO = {"stoa": _pendencias_tracker_stoa}


# --- HIGIENE (1): REAPER DE ÓRFÃOS NO BOOT DA LANE (Camada A, process-based) --------
# O motor (aula/motor/tracker.py) tem `reap_orphans` (Camada B): reset STALE de
# LEGENDA_INFLIGHT por TEMPO. Ele NÃO toca os in-flight ASSÍNCRONOS — a limpeza deles
# é de PROCESSO ("Camada A, no runner da lane"), que nunca era feita. Uma captura async
# que MORREU (crash/redeploy) deixa a aula presa em `transcrevendo`/`capturando_nao_video`
# etc. para SEMPRE: só o passe DONO daquele estado a retoma, e ele não roda se o processo
# caiu. Este reaper roda no BOOT da lane e, para um curso cujo processo de captura NÃO
# está vivo, devolve essas órfãs a `pendente`.
#
# ESPELHO de motor/tracker.py::ASYNC_INFLIGHT (a FONTE-VERDADE vive lá; se mudar, ESTA
# lista muda junto — contrato de espelho, igual a _PENDING_EXCLUDED acima).
_ASYNC_INFLIGHT = ("transcrevendo", "transcrevendo_embed", "capturando_nao_video",
                   "transcrevendo_youtube")


def reap_orphans_local(curso_url, motor_dir, *, curso_ativo, plataforma=None) -> int:
    """REAPER de órfãos ASSÍNCRONOS no BOOT da lane. Uma aula in-flight async
    (transcrevendo*/capturando_nao_video) cujo PROCESSO de captura NÃO está vivo volta a
    `pendente` (re-selecionável). Devolve quantas resetou.

    PLATAFORMAS POR TENANT (`_ESCOPO_TENANT`: Cademí, Entrega Digital) => NO-OP (0), de
    propósito: (a) o passe ÚNICO delas já re-seleciona o próprio in-flight
    (`transcrevendo`/`capturando_nao_video` estão no pool do motor — motor/cademi/
    pipeline.py::_CADEMI_PENDING, motor/entregadigital/pipeline.py::_ED_*_PENDING), então
    não há órfã a resgatar; (b) o course_id delas NÃO é o `/products/<id>` do Hotmart —
    uma URL da Entrega Digital com `/products/123` resetaria aulas do curso Hotmart 123
    (outra conta, possivelmente VIVA). `plataforma=None` => comportamento de sempre.

    DENTES / INVIOLÁVEIS:
      - `curso_ativo(curso_url)` True (captura VIVA) => NO-OP (retorna 0). NUNCA toca uma
        aula cujo dono ainda roda — mexer no tracker de um motor vivo é corrida + risco de
        double-select. É a Ordem IV (nunca perturba processo vivo).
      - Só toca `_ASYNC_INFLIGHT`; terminais (`no_notion`/`sem_conteudo`/`falhou`…) e
        `pendente` ficam intocados. LEGENDA_INFLIGHT é da Camada B (motor, por tempo) —
        aqui não se mexe.
      - PRESERVA `notion_page_id` e `error` (só o `status` muda): o resume ARQUIVA a
        página órfã antes de recriar (fail-closed — nunca promove a `no_notion` de graça).
      - Fail-open: db ausente / course_id não resolve / erro de SQL => 0 (nunca estoura o
        boot da lane por causa de higiene)."""
    if curso_ativo(curso_url):
        return 0                                            # captura VIVA: intocável
    if plataforma in _ESCOPO_TENANT:
        return 0                                            # o motor retoma o próprio in-flight
    course_id = _course_id_de_url(curso_url)
    if not course_id:
        return 0
    db_path = os.path.join(motor_dir, "tracker.db")
    if not os.path.exists(db_path):
        return 0
    con = None
    try:
        con = sqlite3.connect(db_path, timeout=5.0)
        con.execute("PRAGMA busy_timeout=5000")
        marcadores = ",".join("?" * len(_ASYNC_INFLIGHT))
        cur = con.execute(
            f"UPDATE lessons SET status='pendente', updated_at=? "
            f"WHERE course_id=? AND status IN ({marcadores})",
            (time.time(), course_id, *_ASYNC_INFLIGHT))
        con.commit()
        return cur.rowcount or 0
    except sqlite3.Error:
        return 0
    finally:
        if con is not None:
            con.close()


# --- HIGIENE (2): PENDÊNCIA CAPTURÁVEL (detecta "essencialmente pronto") ------------
# Um curso pode ter `no_notion < total` no Notion PARA SEMPRE sem estar bloqueado: as
# aulas TERMINAIS (sem_conteudo = sem conteúdo real; falhou/audio_erro/nao_video_erro =
# falha DETERMINÍSTICA per-aula, K falhas idênticas) nunca chegam ao Notion. Falso-
# positivo real: invistodireito = 533 no_notion + 13 sem_conteudo + 1 falhou (total 547)
# -> Notion nunca "completo" (533<547), mas ZERO trabalho capturável.
#
# CONSERVADOR de propósito (viés a NÃO-pronto): só entram no set TERMINAL os estados
# inequivocamente sem trabalho de captura restante. Uma PAREDE/throttle deixa a aula num
# estado PENDENTE/in-flight (o motor só promove a terminal após K falhas IDÊNTICAS de
# CONTEÚDO) => conta como capturável => o curso NÃO é "pronto" e SEGUE escalando (a
# invariante do cooldown-de-concluído). Estados benignos que passes POSTERIORES retomam
# (sem_legenda→áudio, sem_video→embed, sem_embed→não-vídeo, video_youtube→youtube) NÃO
# são terminais aqui: há trabalho capturável.
_TERMINAIS_DONE = frozenset({
    "no_notion", "anexos_baixados",        # sucesso terminal
    "sem_conteudo",                        # sem conteúdo real (benigno terminal)
    "falhou", "audio_erro", "nao_video_erro",  # falha DETERMINÍSTICA per-aula (K idênticas)
})
# EXCEÇÃO ao terminal `nao_video_erro` (espelha `_pendencias_tracker`): as com a
# assinatura YouTube-resgate (_SQL_YOUTUBE_RESGATE: 'provedor não suportado'+'youtube')
# são TRABALHO do passe --youtube — o rodízio as conta como pool capturável. Sem esta
# exceção, um curso SÓ com elas (ciro-gestor: 68 aulas) seria 'done' aqui, entraria no
# cooldown-de-concluído e o resgate --youtube MORRERIA de starvation (o rodízio nunca
# mais o veria). Elas somam de volta ao capturável em `pendencia_capturavel_local`.


# --- ESCOPO DE TRACKER DAS PLATAFORMAS POR TENANT (Cademí, Entrega Digital) ----------
# No daemon, o "curso" delas é o TENANT inteiro: a URL do YAML é a raiz do tenant e UM
# run do motor enumera todos os cursos/produtos dele. O tracker chaveia cada curso por um
# course_id NAMESPACED — NUNCA pelo `/products/<id>` do Hotmart (`_course_id_de_url`): a
# SPA da Entrega Digital tem rota `products/<id>`, e ler `course_id='<id>'` contaria (ou,
# no reaper, mexeria em) aulas de um curso Hotmart de mesmo número. A leitura é por
# PREFIXO (o tenant inteiro), terminado em ':' para `cademi:a.com.br:` não casar
# `cademi:a.com.br.b:`; comparação EXATA (`substr`), não LIKE (que ignora caixa e trata
# '_' como curinga). ESPELHOS — a fonte-verdade vive no motor; mudou lá, muda aqui
# (mesmo contrato de `_PENDING_EXCLUDED`):
#   - Cademí: motor/cademi/enumerate.py::tenant_of (host INTEIRO, minúsculo, "www."
#     mantido) + make_course_id => `cademi:<host>:<id>`;
#   - Entrega Digital: motor/entregadigital/api.py::tenant_of + enumerate.make_course_id
#     => `entregadigital:<tenant>:product:<pid>`, <tenant> = a 1ª label em
#     `<tenant>.entregadigital.app.br` (o único formato que o gate e `hosts` aceitam
#     hoje); num domínio próprio, o host inteiro (regra da rodada 9 do motor — a 1ª
#     label sozinha, `membros`, colidiria entre clientes).
_ED_SAAS = "entregadigital.app.br"


def _host_do_tenant(curso_url):
    try:
        return (urlparse(str(curso_url or "")).hostname or "").strip().lower()
    except ValueError:                                     # URL malformada: sem escopo
        return ""


def _prefixo_cademi(curso_url):
    host = _host_do_tenant(curso_url)
    return f"cademi:{host}:" if host else None


def _prefixo_entregadigital(curso_url):
    host = _host_do_tenant(curso_url)
    tenant = host.split(".")[0] if host.endswith("." + _ED_SAAS) else host
    return f"entregadigital:{tenant}:product:" if tenant else None


_ESCOPO_TENANT = {"cademi": _prefixo_cademi, "entregadigital": _prefixo_entregadigital}

# Terminais EXTRAS por plataforma de tenant (os pipelines dos dois motores: o passe
# re-seleciona só pendente/transcrevendo[/capturando_nao_video]; `sem_audio` — aula sem
# player capturável — é TERMINAL lá, nenhum passe a retoma). Somados a `_TERMINAIS_DONE`.
# Estado desconhecido continua CAPTURÁVEL (viés a não-pronto, como no Hotmart).
_TERMINAIS_EXTRA_TENANT = {"cademi": frozenset({"sem_audio"}),
                           "entregadigital": frozenset({"sem_audio"})}

# EXCEÇÃO ao terminal `sem_audio` no Cademí (irmã do resgate YouTube): o run do motor
# chama `tracker.requeue_sem_audio_legacy` por curso (motor/cademi/pipeline.py), que volta
# a `pendente` as `sem_audio` com a assinatura LEGADA (motor/tracker.py::
# SEM_AUDIO_LEGACY_SIG). Contá-las como terminais deixaria o tenant 'pronto' e o resgate
# nunca rodaria (o curso 'pronto' não é mais disparado). Espelho da assinatura:
_SEM_AUDIO_LEGACY_SIG = ("nenhum player Panda capturável", "nenhum player Wistia capturável")
_SQL_SEM_AUDIO_LEGADO_TENANT = (
    "SELECT COUNT(*) FROM lessons WHERE substr(course_id, 1, ?) = ? "
    "AND status='sem_audio' AND (" + " OR ".join(
        "error LIKE ?" for _ in _SEM_AUDIO_LEGACY_SIG) + ")")
_RESGATE_SEM_AUDIO_TENANT = frozenset({"cademi"})

# CURSO CONHECIDO SEM AULA NENHUMA no tenant: o run do Cademí faz `upsert_course` de todo
# curso enumerado (motor/orchestrator.py::run_course) mesmo quando a sidebar não deu aula
# (o 302 do /modulo/ caiu numa página de erro — achado C3). Esse curso não tem linha em
# `lessons`, então a soma do tenant o ignoraria e o tenant pareceria 'pronto' com um
# curso INTEIRO por capturar. Presença de um desses => DESCONHECIDO (None), nunca 0.
_SQL_CURSO_SEM_AULA_TENANT = (
    "SELECT COUNT(*) FROM courses c WHERE substr(c.course_id, 1, ?) = ? AND NOT EXISTS "
    "(SELECT 1 FROM lessons l WHERE l.course_id = c.course_id)")


@dataclass(frozen=True)
class _EscopoTracker:
    """ONDE a leitura do tracker procura as aulas de UM curso do daemon. `where`/`params`
    filtram `lessons`; `terminais` = o que NÃO é trabalho; `resgate_sql`/`resgate_params`
    somam de volta um subconjunto terminal que o motor ainda retoma; `prefixo` só existe
    no escopo de tenant (liga a checagem de curso sem aula). Todo SQL aqui é LITERAL do
    código — a URL entra só como parâmetro."""
    where: str
    params: tuple
    terminais: frozenset
    resgate_sql: str = ""
    resgate_params: tuple = ()
    prefixo: str = ""


def _escopo_tracker(curso_url, plataforma=None):
    """Escopo da leitura de pendência do curso. Plataforma de tenant (`_ESCOPO_TENANT`)
    => prefixo do tenant, SEM cair no `/products/` do Hotmart (URL sem host => None).
    Qualquer outra (inclusive None, o chamador antigo) => o course_id `/products/<id>`
    de sempre, com o resgate YouTube — o MESMO SQL e os MESMOS parâmetros de antes."""
    prefixo_fn = _ESCOPO_TENANT.get(plataforma)
    if prefixo_fn is not None:
        prefixo = prefixo_fn(curso_url)
        if not prefixo:
            return None
        faixa = (len(prefixo), prefixo)
        resgate_sql, resgate_params = "", ()
        if plataforma in _RESGATE_SEM_AUDIO_TENANT:
            resgate_sql = _SQL_SEM_AUDIO_LEGADO_TENANT
            resgate_params = faixa + tuple(f"%{s}%" for s in _SEM_AUDIO_LEGACY_SIG)
        return _EscopoTracker(
            "substr(course_id, 1, ?) = ?", faixa,
            _TERMINAIS_DONE | _TERMINAIS_EXTRA_TENANT.get(plataforma, frozenset()),
            resgate_sql, resgate_params, prefixo)
    course_id = _course_id_de_url(curso_url)
    if not course_id:
        return None
    return _EscopoTracker("course_id=?", (course_id,), _TERMINAIS_DONE,
                          _SQL_YOUTUBE_RESGATE, (course_id,))


def pendencia_capturavel_local(curso_url, motor_dir, plataforma=None):
    """Nº de aulas com trabalho de captura AINDA pendente no tracker local (status NÃO
    em `_TERMINAIS_DONE`, MAIS as `nao_video_erro` de resgate YouTube — ver a exceção
    acima). 0 => "essencialmente pronto" (só restam terminais).

    `plataforma` (opcional; o main() passa a do YAML) escolhe o ESCOPO (`_escopo_tracker`):
    Cademí/Entrega Digital somam o TENANT inteiro pelo prefixo do course_id, com os
    terminais do motor delas (`sem_audio` incluso; no Cademí as `sem_audio` legadas
    voltam como capturáveis); as demais, o `/products/<id>` de sempre.

    Devolve **None** (DESCONHECIDO => fail-open, o chamador NÃO conclui) quando: db
    ausente, course_id não resolve, erro de SQL, **ou o curso não tem NENHUMA linha** no
    tracker (nunca semeado localmente). Este último caso é CRÍTICO: 0-linhas != pronto —
    tratá-lo como 0-pendente marcaria um curso NUNCA capturado como concluído e o
    STARVARIA. Só um curso COM linhas e SEM capturável é 'pronto'. No escopo de tenant,
    idem para um curso do tenant conhecido em `courses` e SEM aula (`_SQL_CURSO_SEM_AULA_
    TENANT`): o tenant não é 'pronto' com um curso inteiro por capturar."""
    escopo = _escopo_tracker(curso_url, plataforma)
    if escopo is None:
        return None
    db_path = os.path.join(motor_dir, "tracker.db")
    if not os.path.exists(db_path):
        return None
    con = None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        con.execute("PRAGMA busy_timeout=5000")
        linhas = con.execute(
            f"SELECT status, COUNT(*) FROM lessons WHERE {escopo.where} GROUP BY status",
            escopo.params).fetchall()
        # BLOQUEANTE-1 do review: resgate YouTube conta como CAPTURÁVEL (não terminal).
        # (no Cademí, o resgate das `sem_audio` legadas — mesma razão.)
        resgate = (con.execute(escopo.resgate_sql, escopo.resgate_params).fetchone()[0]
                   if escopo.resgate_sql else 0)
        sem_aula = (con.execute(_SQL_CURSO_SEM_AULA_TENANT, escopo.params).fetchone()[0]
                    if escopo.prefixo else 0)
    except sqlite3.Error:
        return None
    finally:
        if con is not None:
            con.close()
    if not linhas or sem_aula:
        return None                                        # nunca semeado: NÃO é 'pronto'
    return sum(n for (s, n) in linhas if s not in escopo.terminais) + resgate


def _passe_ativavel(passe, plataforma="hotmart"):
    """Um passe pode ser DISPARADO? Gates de ativação POR FLAG (mesmo mecanismo p/ todos:
    o código fica WIRED e a operação liga por env + restart guardado, sem tocar código):
      - STOA: os passes NOVOS (`embed`/`nao-video` — vídeo cross-plataforma e doc→Notion,
        as ferramentas 2 e 3) ligam JUNTOS com `ATHENA_STOA_PASSES_ATIVO`. Desligado =>
        comportamento vivo de hoje (só o `base` áudio-nativo) — nada muda até a ativação
        deliberada. O gate NÃO afeta o anti-ban (1 motor/conta é o lock durável).
      - Hotmart: `nao-video` liga com `ATHENA_NAO_VIDEO_ATIVO` (o gate original do braço
        doc->Notion da remediação #2); `youtube` (braço YouTube, deploy 3517c47 do motor)
        liga com `ATHENA_YOUTUBE_ATIVO` — desligado => comportamento vivo de hoje. Os
        demais passes são sempre ativáveis."""
    if plataforma == "stoa":
        if passe in ("embed", "nao-video"):
            return bool(os.getenv("ATHENA_STOA_PASSES_ATIVO"))
        return True
    if passe == "nao-video":
        return bool(os.getenv("ATHENA_NAO_VIDEO_ATIVO"))
    if passe == "youtube":
        return bool(os.getenv("ATHENA_YOUTUBE_ATIVO"))
    return True


def _spawn_popen(cmd, *, env, cwd):  # pragma: no cover — processo REAL do motor
    """Spawn REAL não-bloqueante (o motor roda minutos-horas; o disparo retorna já).
    `start_new_session` desacopla a captura do processo da Athena (uma reinicialização
    do loop não mata uma captura em andamento).

    STDERR FIADO (a causa-raiz do flap vive AQUI): se `env[_ENV_STDERR_TEE]` aponta um
    arquivo, stdout+stderr do filho são TEE'D para ele (o motor imprime tanto os abortos
    controlados — 'SESSÃO MORTA', 'RUN INTERROMPIDO' — no stdout quanto tracebacks no
    stderr; capturar OS DOIS é o que dá à autópsia/causa-raiz o sinal real, em vez de
    'causa desconhecida'). Sem a chave, cai no DEVNULL enxuto de antes (retrocompat). A
    chave é REMOVIDA do env do filho — é fiação do supervisor, não config do motor."""
    import subprocess
    env = dict(env)
    err_path = env.pop(_ENV_STDERR_TEE, None)
    saida = subprocess.DEVNULL
    if err_path:
        try:
            os.makedirs(os.path.dirname(err_path), exist_ok=True)
            saida = open(err_path, "wb")   # trunca a cada disparo: o TAIL é do run atual
        except OSError:
            saida = subprocess.DEVNULL
    try:
        return subprocess.Popen(cmd, env=env, cwd=cwd, stdout=saida,
                                stderr=subprocess.STDOUT, start_new_session=True)
    finally:
        # o filho já dup'ou o fd; o pai fecha a sua cópia para não vazar handle.
        if saida not in (subprocess.DEVNULL, None):
            try:
                saida.close()
            except OSError:
                pass


def _pid_vivo(pid) -> bool:
    """O PID ainda roda? `os.kill(pid, 0)` NÃO envia sinal — só sonda a existência do
    processo. ProcessLookupError => morreu (lock órfão obsoleto, pode liberar).
    PermissionError => existe mas é de outro dono (vivo, conservador: NÃO libera).
    Qualquer outro OSError => trata como morto (fail-open p/ NÃO travar a conta para
    sempre por um pid ilegível). pid inválido (None/0/negativo) => morto."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# Diretório de locks DURÁVEIS padrão. A verdade do guard anti-ban vive AQUI e tem de
# sobreviver não só a um restart/crash do loop, mas a um REBOOT do Mac — por isso mora no
# HOME, NÃO no tempdir do SO. Em macOS `tempfile.gettempdir()` devolve /var/folders/.../T
# (e /tmp), ambos VARRIDOS no reboot: um lock ali seria apagado e um curso ainda em
# captura (ou re-semeado) poderia ser re-disparado na mesma conta = ban. O HOME persiste.
# (Env ATHENA_LOCK_DIR sobrepõe; ver LocalExecutor / athena_local.main.)
_LOCK_DIR_PADRAO = os.path.join(os.path.expanduser("~"), ".athena-local", "locks")

# Onde o stderr+stdout de CADA motor spawnado é tee'd (um arquivo por CONTA, sobrescrito
# a cada disparo — o TAIL é sempre do run mais recente). Mora sob o mesmo ~/.athena-local
# do lock/autópsia. É a EVIDÊNCIA que a autópsia (vigia) lê via `stderr_path` para a
# causa-raiz deixar de ser 'desconhecida'. Env ATHENA_MOTOR_LOG_DIR sobrepõe.
_MOTOR_LOG_DIR_PADRAO = os.path.join(
    os.path.expanduser("~"), ".athena-local", "motor-logs")

# EVIDÊNCIA COPIADA NO REAP (fix do incidente 10/09 23:33–23:55): o .err é POR CONTA e o
# `_spawn_popen` o TRUNCA a cada disparo. Guardar só o path no óbito deixava a autópsia
# ler o arquivo DEPOIS — e, entre o reap (que roda em qualquer consulta ao executor,
# inclusive no meio da passada) e a autópsia do ciclo seguinte, a conta era relançada e o
# .err passava a ser do run NOVO (a morte por TimeoutError virava "causa desconhecida").
# Por isso o `_reap` copia a CAUDA no momento em que colhe o filho: antes de soltar o lock
# e, portanto, antes de qualquer relançamento da conta. Só os últimos bytes (o .err de um
# run de horas tem MBs; o vigia usa as últimas ~40 linhas).
_CAUDA_REAP_BYTES = 64 * 1024
# Folga entre o disparo e o mtime do .err do MESMO run: o spawn trunca o .err logo depois
# de o disparo ser carimbado. Um .err mais velho que o disparo além disso é de um run
# ANTERIOR (o tee falhou neste disparo e caiu no DEVNULL) — não é evidência desta morte.
# Mesmo critério do `vigia._ERR_FOLGA_S` (morte só-de-lock).
_CAUDA_FOLGA_S = 5.0


def _cauda_do_err(path, desde=None) -> str:
    """Últimos `_CAUDA_REAP_BYTES` do .err do run que acabou de ser colhido. "" quando não
    há evidência DESTE run: arquivo ausente/ilegível, ou mais velho que o disparo `desde`
    (sobra de um run anterior). Nunca levanta — a autópsia segue com cauda vazia (e a causa
    fica fail-closed, honesta), jamais com a cauda de outro run."""
    try:
        if desde is not None and os.path.getmtime(path) < desde - _CAUDA_FOLGA_S:
            return ""
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            tamanho = f.tell()
            inicio = max(0, tamanho - _CAUDA_REAP_BYTES)
            f.seek(inicio)
            bruto = f.read()
    except OSError:
        return ""
    texto = bruto.decode("utf-8", errors="replace")
    if inicio > 0:
        texto = texto.split("\n", 1)[1] if "\n" in texto else ""   # 1ª linha veio cortada
    return texto


# Arquivos de singleton que o Chrome cria DENTRO do user-data-dir. Um crash/SIGKILL do
# motor pode deixar o SingletonLock ÓRFÃO; o próximo run da MESMA conta então falharia com
# "Failed to create a ProcessSingleton ... File exists (17)" mesmo SEM concorrência. Como
# o guard 1-por-conta garante que, ao disparar, NENHUM motor daquela conta está vivo, um
# Singleton* presente no perfil dela é necessariamente OBSOLETO e pode ser removido.
_SINGLETON_NOMES = ("SingletonLock", "SingletonSocket", "SingletonCookie")


def _perfil_de_conta(conta) -> str:
    """Diretório de perfil Chrome DEDICADO da conta (RELATIVO ao cwd=motor_dir, como os
    perfis de Stoa/Kajabi). Sanitiza a conta p/ um nome de dir seguro. Contas distintas =>
    perfis distintos => nunca colidem no ProcessSingleton do Chrome (a causa do flap)."""
    slug = "".join(c if (c.isalnum() or c in "-_") else "-" for c in str(conta)) or "conta"
    return ".chrome-profile-" + slug


def _caminho_normalizado(cwd, caminho) -> str:
    """`caminho` como o MOTOR o abre (relativo => relativo ao seu cwd), normalizado só
    por STRING (normpath): NENHUM acesso ao sistema de arquivos — é usado para comparar
    arquivos de sessão, que o daemon nunca abre nem sonda."""
    return os.path.normpath(os.path.join(str(cwd), str(caminho)))


def _limpar_singleton_orfao(profile_dir):  # pragma: no cover — I/O de arquivo real
    """Remove Singleton* ÓRFÃOS do `profile_dir` (crash deixou o lock). Chamado só no
    disparo, quando o guard 1-por-conta já garante que nenhum motor da conta está vivo —
    logo o lock é obsoleto. Best-effort: falha de remoção não impede o disparo."""
    if not profile_dir or not os.path.isdir(profile_dir):
        return
    for nome in _SINGLETON_NOMES:
        p = os.path.join(profile_dir, nome)
        try:
            os.remove(p)
        except OSError:
            pass


class LocalExecutor:
    """Executor DOMÉSTICO: CHAMA O MOTOR DIRETO no Mac (subprocesso), em vez de
    enfileirar. Mesmo contrato do FilaExecutor: `disparar(url) -> confirmação truthy`,
    ou LEVANTA em falha.

    INVIOLÁVEIS cravados AQUI (o executor é o único ponto que SABE como a captura
    acontece — logo é o lugar certo do guard físico anti-ban):
      - 1-POR-CONTA, DURÁVEL: nunca 2 capturas na MESMA conta ao mesmo tempo, INCLUSIVE
        através de um restart/crash do loop. Um 2º disparo na conta ocupada LEVANTA
        `ContaOcupada` (fail-closed). Contas diferentes rodam em paralelo.
      - IDEMPOTENTE POR CURSO, DURÁVEL: um curso cujo processo AINDA roda não é
        re-spawnado (nem martela, nem duplica a captura); devolve confirmação sem abrir
        um 2º processo — mesmo se quem disparou foi uma ENCARNAÇÃO ANTERIOR do executor.
      - WHISPER_BACKEND=groq SEMPRE (a GROQ_API_KEY vem do chave-groq.txt, que
        `motor.config` auto-carrega quando o subprocesso roda com cwd=motor_dir).
      - Hotmart HEADED (a sonda de sessão FALHA headless); demais plataformas headless.
      - NUNCA loga sozinho: o executor só dispara o motor; a sessão (e o reseed HEADED
        interativo no Mac) é responsabilidade do próprio motor (`ensure_session`).

    POR QUE O GUARD É DURÁVEL EM DISCO (e não só o dict in-memory `self._procs`):
    `_spawn_popen` usa `start_new_session=True` DE PROPÓSITO — a captura (horas) SOBREVIVE
    a um restart do loop. Se o único guard fosse o dict in-memory, um crash/OOM/redeploy do
    loop nasceria um LocalExecutor novo com `_procs={}`, releria o Notion, veria o curso
    PARCIAL, acharia a conta livre e DISPARARIA A MESMA captura DE NOVO — 2 sessões de
    browser na MESMA conta = BAN + captura dupla (o exato prejuízo do inviolável anti-ban).
    Por isso cada disparo grava um LOCKFILE por conta em disco (PID + course_url); antes de
    disparar, consulta-se o lock e checa-se se o PID AINDA RODA (`_pid_vivo`). O lock
    sobrevive ao restart; um PID morto (processo encerrou) libera a conta (lock obsoleto é
    removido). `self._procs` continua existindo só para colher (poll()) os processos que
    ESTA encarnação spawnou — reaping preciso do próprio filho, evitando zumbi manter a
    conta 'ocupada'. A verdade do guard, porém, é o disco (atravessa restart).

    A completude/monitoramento NÃO é daqui (não há fila/tracker no Mac): é do OWNER
    (`orquestrador.orquestrar_captura` via a contagem-verdade do Notion). O executor só
    sabe o que está VIVO agora (`curso_ativo`/`conta_ocupada`), colhendo processos
    encerrados a cada consulta (reap por `poll()` + varredura de lock obsoleto)."""

    def __init__(self, cursos, *, motor_python, motor_dir, spawn=None, groq_key=None,
                 extra_env=None, lock_dir=None, pid_vivo=None,
                 motor_dir_por_plataforma=None, motor_log_dir=None, pendencias_fn=None,
                 portao_carga=None):
        self._meta = {c.url: c for c in cursos}
        # PORTÃO DE CARGA (maestro.carga.PortaoCarga | None): consultado em `disparar`
        # depois do guard anti-ban e antes de qualquer spawn. None (default) = sem portão —
        # os testes nunca leem a carga REAL desta máquina; o `main()` liga o do ambiente.
        self._portao_carga = portao_carga
        self._motor_python = motor_python
        self._motor_dir = motor_dir
        # SEAM da escolha de passe: lê pendências do tracker p/ decidir QUAL passe rodar.
        # Injetável nos testes (o injetado vale p/ TODAS as plataformas). Sem injeção
        # (None), o default é resolvido POR PLATAFORMA no `_escolher_passe` (Hotmart =
        # `_pendencias_tracker` por course_id; Stoa = `_pendencias_tracker_stoa` agregado).
        # None em qualquer erro => round-robin cego (fail-open p/ rodízio).
        self._pendencias_fn = pendencias_fn
        # Cursor do ANEL de passes POR CONTA (anti-fome: alterna entre os passes elegíveis
        # em vez de fixar sempre o 1º). In-memory: perda no restart é benigna (recomeça do
        # início do anel). NÃO afeta o anti-ban (o guard é o lock durável em disco).
        self._passe_cursor = {}
        # Override de diretório do motor POR PLATAFORMA (ex.: Stoa vive no worktree
        # adaptador-stoa, não em /aula). Default = self._motor_dir para as demais.
        self._motor_dir_por_plataforma = dict(motor_dir_por_plataforma or {})
        self._spawn = spawn or _spawn_popen
        self._groq_key = groq_key
        self._extra_env = dict(extra_env or {})
        self._procs = {}                       # course_url -> handle de processo vivo
        # STDERR FIADO: por CONTA, o arquivo onde o motor daquela conta tee'a stdout+stderr.
        # `disparar` grava; `_reap` anexa o path ao óbito (a autópsia lê o TAIL de lá). Por
        # conta (não por curso) porque o óbito e o guard anti-ban são chaveados por conta.
        self._motor_log_dir = motor_log_dir or _MOTOR_LOG_DIR_PADRAO
        # ÓBITOS colhidos: {conta -> {conta, course_url, exit_code, pid}}. Populado no
        # `_reap` quando um filho encerra; DRENADO (e zerado) pelo loop a cada ciclo para
        # alimentar a autópsia (maestro.vigia). Guardar o exit_code REAL do filho ANTES de
        # descartar o handle dá à causa-raiz (maestro.causa) o sinal FORTE de morte.
        self._obitos = {}
        # Carimbo do disparo por curso (antes do spawn, que trunca o .err): o `_reap` só
        # aceita como evidência um .err que não seja mais velho que ELE (ver `_cauda_do_err`).
        self._disparo_ts = {}
        # Guard DURÁVEL: dir estável (sobrevive a restart) + sonda de PID injetável (os
        # testes de RESTART simulam o processo antigo ainda vivo sem um pid real).
        self._lock_dir = lock_dir or _LOCK_DIR_PADRAO
        self._pid_vivo = pid_vivo or _pid_vivo
        os.makedirs(self._lock_dir, exist_ok=True)

    # --- LOCKFILE DURÁVEL POR CONTA (a verdade do guard anti-ban) ------------
    def _lock_path(self, conta) -> str:
        # nome de arquivo estável e seguro a partir da conta (que pode ter espaços/barras).
        slug = hashlib.sha256(str(conta).encode("utf-8")).hexdigest()[:16]
        return os.path.join(self._lock_dir, slug + ".lock")

    def _stderr_path(self, conta) -> str:
        """Arquivo de stderr+stdout tee'd do motor DESTA conta (mesmo slug do lock, para
        casar 1:1 com o guard anti-ban por conta). É o que `disparar` passa ao spawn e o
        que `_reap` anexa ao óbito (a autópsia lê o TAIL)."""
        slug = hashlib.sha256(str(conta).encode("utf-8")).hexdigest()[:16]
        return os.path.join(self._motor_log_dir, slug + ".err")

    def _ler_lock(self, conta, *, limpar=True):
        """Lê o lock da conta e devolve o dict {pid, course_url, conta} SE o PID ainda
        roda; senão devolve None e REMOVE o lock obsoleto (processo morreu -> conta
        livre). É aqui que o restart libera uma conta cujo processo antigo já terminou.

        `limpar=False` = SÓ LEITURA: mesma resposta (vivo/intenção -> dict; morto/ilegível
        -> None), mas NÃO apaga o lock obsoleto. Um lock de PID MORTO de uma encarnação
        anterior é a ÚNICA evidência dessa morte para a autópsia (vigia.autopsia varre o
        lock_dir no 1º ciclo); quem consulta a liveness ANTES dessa varredura (o boot da
        lane) tem de usar só-leitura, senão a morte some sem autópsia."""
        path = self._lock_path(conta)
        try:
            with open(path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return None
        except (ValueError, OSError):
            # lock corrompido/ilegível: trata como obsoleto (não pode travar para sempre).
            if limpar:
                self._remover_lock(path)
            return None
        if data.get("pid") is None:
            # LOCK DE INTENÇÃO (gravado ANTES do spawn, ver `disparar`): FAIL-CLOSED. Ou o
            # spawn está acontecendo AGORA (mesma encarnação, síncrono), ou um crash caiu na
            # janela intenção->PID e deixou uma captura ÓRFÃ cujo PID não conhecemos. Nos
            # dois casos a conta está OCUPADA — tratar como livre re-dispararia (2 browsers
            # na mesma conta = ban). Sem PID a sondar, NÃO consultamos `_pid_vivo`. Este é o
            # exato ponto que fecha o TOCTOU; o preço é anti-ban > disponibilidade (um lock
            # de intenção de um crash cuja captura também morreu só libera por ação humana —
            # a completude-por-Notion ou o disjuntor/stall do owner escalam esse curso).
            return data
        if not self._pid_vivo(data.get("pid")):
            if limpar:
                self._remover_lock(path)          # PID morto -> lock obsoleto -> libera
            return None
        return data

    def _escrever_lock(self, conta, curso_url, pid):
        tmp = self._lock_path(conta) + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"pid": pid, "course_url": curso_url, "conta": str(conta),
                       "ts": time.time()}, f)
        os.replace(tmp, self._lock_path(conta))   # troca atômica

    def _remover_lock(self, path):
        try:
            os.remove(path)
        except OSError:
            pass

    def _reap(self):
        """Colhe processos que ESTA encarnação spawnou e já encerraram (poll() != None):
        tira do dict e REMOVE o lock durável da conta (se ainda for deste curso), para
        que a próxima captura da conta possa disparar. Sem isso a conta ficaria 'ocupada'
        para sempre. poll() reapa o zumbi do próprio filho — essencial: um filho encerrado
        e não-colhido continuaria 'vivo' para `os.kill(pid, 0)`."""
        for url in [u for u, p in self._procs.items() if p.poll() is not None]:
            proc = self._procs.pop(url)
            disparo_ts = self._disparo_ts.pop(url, None)
            meta = self._meta.get(url)
            if meta is None:
                continue
            err_path = self._stderr_path(meta.conta)
            # ÓBITO: grava o exit_code REAL do filho ANTES de descartar o handle. É o
            # sinal FORTE que a autópsia (vigia) e a causa-raiz (causa) consomem para
            # distinguir saída limpa (exit 0), SIGKILL/OOM (-9/137) e falha genérica.
            self._obitos[str(meta.conta)] = {
                "conta": str(meta.conta), "course_url": url,
                "exit_code": getattr(proc, "returncode", None),
                "pid": getattr(proc, "pid", None),
                "stderr_path": err_path,
                # EVIDÊNCIA COPIADA AGORA (fix 10/09): a cauda do .err da conta no instante
                # do reap — ANTES de soltar o lock abaixo, logo antes de qualquer
                # relançamento que truncaria o arquivo. O vigia usa esta cópia (o
                # `stderr_tail` tem precedência sobre o path); "" = sem evidência DESTE run.
                "stderr_tail": _cauda_do_err(err_path, desde=disparo_ts)}
            path = self._lock_path(meta.conta)
            try:
                with open(path) as f:
                    data = json.load(f)
            except (FileNotFoundError, ValueError, OSError):
                continue
            if data.get("course_url") == url:    # só remove o lock SE ainda for deste curso
                self._remover_lock(path)

    def drenar_obitos(self) -> dict:
        """Colhe (via `_reap`) e ZERA os óbitos dos filhos que ESTA encarnação spawnou e
        que encerraram desde a última drenagem. Devolve {conta -> {conta, curso,
        exit_code, pid, stderr_path, stderr_tail}} — a fonte por-conta que
        `maestro.vigia.autopsia` consome (`_coerce_fonte` aceita o dict). `stderr_tail` é
        a cauda COPIADA no reap (a do run que morreu, mesmo que a conta já tenha sido
        relançada e o .err truncado); o vigia a usa no lugar de reler o path.
        Idempotente entre ciclos: a MESMA morte não volta na drenagem seguinte, então não
        infla o flapping (contrato do vigia)."""
        try:
            self._reap()
        finally:
            # zera SEMPRE (mesmo se o reap estourar no meio): um óbito colhido e nunca
            # drenado voltaria em toda drenagem seguinte.
            obitos, self._obitos = self._obitos, {}
        return {c: {"conta": d["conta"], "curso": d.get("course_url", ""),
                    "exit_code": d.get("exit_code"), "pid": d.get("pid"),
                    "stderr_path": d.get("stderr_path"),
                    "stderr_tail": d.get("stderr_tail")}
                for c, d in obitos.items()}

    def aguardando_autopsia(self, curso_url) -> bool:
        """A CONTA deste curso tem um óbito já colhido (`_reap`) e ainda NÃO drenado pela
        autópsia? Por CONTA — a unidade do anti-ban e do .err. O loop NÃO age sobre essa
        morte antes da autópsia classificá-la (incidente 10/09: o reap no meio da passada
        caía no fallback, que contava a falha e RE-DISPARAVA a conta — e a autópsia do
        ciclo seguinte contava a MESMA morte de novo, depois de o re-disparo já ter
        acontecido, inclusive sobre uma sessão morta). `drenar_obitos` libera."""
        self._reap()
        meta = self._meta.get(curso_url)
        return meta is not None and self._obito_pendente(meta.conta) is not None

    def curso_ativo(self, curso_url, *, limpar=True) -> bool:
        """O curso tem captura VIVA agora? `limpar=False` responde IGUAL mas sem apagar
        lock de PID morto (ver `_ler_lock`) — é o que o boot da lane usa antes da 1ª
        autópsia. O `_reap` segue nos dois modos: ele só colhe filhos DESTA encarnação e
        guarda o óbito (exit_code) em `_obitos` ANTES de soltar o lock — nada se perde."""
        self._reap()
        meta = self._meta.get(curso_url)
        if meta is None:
            return False
        lock = self._ler_lock(meta.conta, limpar=limpar)
        return lock is not None and lock.get("course_url") == curso_url

    def conta_de(self, curso_url) -> str:
        meta = self._meta.get(curso_url)
        return meta.conta if meta is not None else ""

    def _motor_dir_de(self, plataforma) -> str:
        """cwd (e raiz do `python -m`) do motor DESTA plataforma. Default = self._motor_dir
        (o /aula, onde vivem Hotmart/Memberkit/Kajabi). Uma plataforma cujo código-CORRIGIDO
        vive noutra árvore (ex.: Stoa no worktree `adaptador-stoa`) é apontada aqui — sem
        mover o código nem arriscar um merge que contamine o caminho intocado do Hotmart. O
        cwd é ONDE caem os artefatos duráveis da captura (.chrome-profile-*, .stoa-session.
        json, tracker.db): apontar pro worktree é o que REUSA a sessão viva e o tracker
        idempotente na retomada — o handoff do launcher manual sem re-login."""
        return self._motor_dir_por_plataforma.get(plataforma, self._motor_dir)

    def conta_ocupada(self, conta) -> bool:
        self._reap()
        return self._ler_lock(conta) is not None

    def motores_ativos(self) -> int:
        """Quantos motores rodam AGORA, pela verdade-em-disco do guard anti-ban: locks do
        `lock_dir` com PID vivo, ou de INTENÇÃO (pid=None, fail-closed) — inclusive de
        encarnações anteriores (órfãs que sobreviveram ao loop). SÓ LEITURA (não apaga lock
        morto: a autópsia precisa dele). Mesma regra do `controle.capturas_vivas`."""
        from maestro.controle import capturas_vivas
        self._reap()
        return len(capturas_vivas(self._lock_dir, pid_vivo=self._pid_vivo))

    def _veto_de_carga(self):
        """(motivo, escalonamento) do adiamento pelo portão de carga, ou None. Fail-open:
        um portão/contagem que LEVANTA não pode parar a captura (volta ao comportamento sem
        portão). Portão só com `avaliar` (sem `decidir`) = motivo de sobrecarga comum."""
        if self._portao_carga is None:
            return None
        try:
            ativos = self.motores_ativos()
        except Exception:
            ativos = 0
        try:
            decidir = getattr(self._portao_carga, "decidir", None)
            if callable(decidir):
                veto = decidir(ativos)
                if veto is None:
                    return None
                return (str(getattr(veto, "motivo", veto)),
                        bool(getattr(veto, "escalonamento", False)))
            motivo = self._portao_carga.avaliar(ativos)
            return (str(motivo), False) if motivo else None
        except Exception:
            return None

    def _avisar_portao(self, metodo):
        """Chama um gancho do portão de carga (novo_ciclo / registrar_disparo /
        retomar_sobrecarga) sem NUNCA derrubar o chamador (observabilidade/rampa não param
        a captura)."""
        fn = getattr(self._portao_carga, metodo, None)
        if not callable(fn):
            return
        try:
            fn()
        except Exception:
            pass

    def novo_ciclo(self):
        """Fronteira de ciclo do loop -> janela da rampa do portão de carga."""
        self._avisar_portao("novo_ciclo")

    def retomar_sobrecarga(self):
        """O loop reiniciou dentro de um episódio de sobrecarga persistido -> o portão nasce
        em sobrecarga (histerese atravessa o reinício)."""
        self._avisar_portao("retomar_sobrecarga")

    def _obito_pendente(self, conta):
        """O óbito colhido e ainda NÃO drenado da conta (dict), ou None. É a verdade que
        `aguardando_autopsia` (a passada) e `disparar` (o ponto único) consultam."""
        return self._obitos.get(str(conta))

    def disparar(self, curso_url):
        self._reap()
        meta = self._meta.get(curso_url)
        if meta is None:
            # fail-closed: sem conta/plataforma não dá para respeitar o anti-ban nem
            # montar o comando — jamais disparar às cegas.
            raise RuntimeError(
                f"curso {curso_url} sem metadados locais (conta/plataforma) — não disparo")
        # ÓBITO AINDA NÃO AUTOPSIADO (achado r6): logo depois do `_reap`, que acabou de colher
        # qualquer filho morto — inclusive um que morreu DEPOIS do `_aguardando_autopsia` da
        # passada (janela: pendência do tracker com busy_timeout + disjuntor). A conta NÃO
        # dispara antes de a autópsia classificar a morte (numa sessão morta seria mais uma
        # sonda na superfície de ban). Vem ANTES do portão de carga: a causa de não disparar
        # é esta, e o log não mente. O `drenar_obitos` do próximo ciclo libera (espera 1).
        obito = self._obito_pendente(meta.conta)
        if obito is not None:
            raise AguardaAutopsia(
                f"conta {meta.conta!r}: óbito de {obito.get('course_url')} (exit "
                f"{obito.get('exit_code')}) colhido e ainda não autopsiado — não disparo "
                f"{curso_url} antes da causa (espera o próximo ciclo)")
        # GUARD DURÁVEL: a verdade está no disco (sobrevive a restart), não no _procs.
        lock = self._ler_lock(meta.conta)
        if lock is not None:
            if lock.get("course_url") == curso_url:        # IDEMPOTENTE por curso (durável)
                return f"ja_capturando:{curso_url}"
            raise ContaOcupada(                            # ANTI-BAN 1-por-conta (durável)
                f"conta {meta.conta!r} já captura {lock.get('course_url')} (PID "
                f"{lock.get('pid')} vivo) — recuso 2ª captura simultânea de {curso_url} "
                f"(anti-ban: 1 por conta, sobrevive a restart do loop)")
        # PORTÃO DE CARGA (incidente 10/09): máquina sobrecarregada (carga/memória) ou no
        # teto de motores => ADIA, antes de escolher o passe (não avança o rodízio), gravar
        # lock ou spawnar. Vem DEPOIS da idempotência e do anti-ban: se a conta já está
        # ocupada, a causa de não disparar é ELA — o log do adiamento não mente.
        # ESCALONAMENTO (rampa da saída da sobrecarga): o portão já não está sobrecarregado,
        # mas o teto de disparos NOVOS por ciclo foi atingido -> DisparoEscalonado (também
        # adiamento, não falha; o loop não o conta como sobrecarga no episódio do aviso).
        veto = self._veto_de_carga()
        if veto:
            motivo, escalonamento = veto
            if escalonamento:
                raise DisparoEscalonado(motivo)
            raise MaquinaSobrecarregada(motivo)
        # ESCOLHA DO PASSE (por demanda, com rodízio anti-fome). Feita AQUI, DEPOIS do guard
        # anti-ban e ANTES do único spawn: muda só o ARGV do processo — jamais a QUANTIDADE
        # (1 spawn). Não pode disparar 2 passes na mesma conta: o passe N+1 só é escolhido
        # quando `disparar` é chamado de novo, e o lock durável já barrou isso enquanto o
        # motor do passe N não morreu e foi colhido (`_reap`).
        passe = self._escolher_passe(meta)
        cmd, env, cwd = self._montar(meta, passe)          # fail-closed ANTES de qualquer spawn
        # STDERR FIADO: diz ao spawn default ONDE tee'ar stdout+stderr do motor (por conta).
        # O `_spawn_popen` POPA esta chave antes do exec — o motor não a herda. Spawns
        # injetados (testes) a ignoram (contrato `(cmd, *, env, cwd)` intacto).
        env[_ENV_STDERR_TEE] = self._stderr_path(meta.conta)
        # SINGLETON ÓRFÃO: se um run anterior DESTA conta crashou, pode ter deixado o
        # SingletonLock no perfil — e o próximo run falharia na largada mesmo sem
        # concorrência. O guard 1-por-conta já provou que nenhum motor da conta está vivo
        # AGORA, então qualquer Singleton* no perfil dela é obsoleto: limpa antes de subir.
        perfil = env.get("CHROME_USER_DATA_DIR")
        if perfil:
            _limpar_singleton_orfao(perfil if os.path.isabs(perfil)
                                    else os.path.join(cwd, perfil))
        # TOCTOU (fix do achado MÉDIO): grava o lock de INTENÇÃO (pid=None) ANTES do spawn.
        # Se o loop crashar na janela sub-ms entre o spawn e a escrita do PID, a captura
        # órfã (start_new_session) segue viva SEM que seu PID tenha sido registrado — mas o
        # lock de intenção JÁ está em disco e (via `_ler_lock` pid=None fail-closed) mantém
        # a conta OCUPADA no restart, de modo que NADA re-dispara. Gravar o lock só DEPOIS
        # do spawn (o bug) deixaria essa janela sem lock -> re-disparo -> ban + captura dupla.
        self._escrever_lock(meta.conta, curso_url, None)
        # carimbo do disparo ANTES do spawn (que trunca o .err): o reap só aceita como
        # evidência deste run um .err que não seja mais velho que isto (`_cauda_do_err`).
        self._disparo_ts[curso_url] = time.time()
        try:
            proc = self._spawn(cmd, env=env, cwd=cwd)
        except Exception:
            # o spawn FALHOU: a intenção não virou captura. Remove o lock de intenção para
            # não travar a conta para sempre por um disparo que nunca aconteceu (o processo
            # não existe; manter o lock seria uma conta ocupada por nada).
            self._remover_lock(self._lock_path(meta.conta))
            self._disparo_ts.pop(curso_url, None)
            raise
        self._procs[curso_url] = proc
        # PROMOVE o lock de intenção a lock DEFINITIVO, com o PID real do processo de
        # captura — é o que um executor nascido pós-restart lerá (via `_pid_vivo`) para
        # decidir se a conta ainda está ocupada ou já pode ser liberada/retomada.
        self._escrever_lock(meta.conta, curso_url, getattr(proc, "pid", None))
        # um motor SUBIU: conta na janela da rampa do portão (só pesa na saída da sobrecarga)
        self._avisar_portao("registrar_disparo")
        return f"local_iniciada:{curso_url}:passe={passe}"

    def _escolher_passe(self, meta):
        """Escolhe UM passe para ESTE disparo, POR DEMANDA (o que tem pendência no tracker)
        com RODÍZIO anti-fome (cursor por conta). Plataforma de passe único (áudio-nativos)
        => sempre "base" (no-op). Erro de leitura => round-robin CEGO nos passes ativáveis
        (fail-open p/ RODÍZIO, nunca paralelismo). Nenhum passe elegível => "base" (sonda de
        re-enumeração barata e sempre válida; se nada há a fazer, sai limpo e o cooldown do
        owner quiesce o curso). NUNCA muda a QUANTIDADE de spawns — só o argv do único."""
        spec = _PLATAFORMAS.get(meta.plataforma)
        passes = spec.passes if spec is not None else ("base",)
        if len(passes) == 1:
            return passes[0]                               # áudio-nativos: NADA muda
        ativaveis = [p for p in passes if _passe_ativavel(p, meta.plataforma)]
        if len(ativaveis) == 1:
            # Um único passe ativável (ex.: Stoa com o gate ATHENA_STOA_PASSES_ATIVO
            # desligado => só "base"): a escolha está decidida — NÃO consulta pendências
            # (poupa a query SQLite e mantém o caminho de execução IDÊNTICO ao de antes
            # da fiação multi-passe enquanto o gate não liga; achado do review).
            return ativaveis[0]
        fn = self._pendencias_fn or _PENDENCIAS_PADRAO.get(
            meta.plataforma, _pendencias_tracker)
        try:
            pend = fn(meta.url, self._motor_dir_de(meta.plataforma))
        except Exception:
            pend = None
        if pend is None:
            candidatos = ativaveis                         # cego: rodízio nos ativáveis
        else:
            candidatos = [p for p in ativaveis if pend.get(p, 0) > 0]
        if not candidatos:
            return "base"                                  # nada elegível: sonda base
        return self._proximo_no_anel(meta.conta, passes, candidatos)

    def _proximo_no_anel(self, conta, passes, candidatos):
        """Próximo passe elegível no ANEL `passes` a partir do cursor da conta; avança o
        cursor. Garante alternância (anti-fome) entre os candidatos ao longo dos disparos."""
        cur = self._passe_cursor.get(conta, 0) % len(passes)
        for i in range(len(passes)):
            idx = (cur + i) % len(passes)
            if passes[idx] in candidatos:
                self._passe_cursor[conta] = (idx + 1) % len(passes)
                return passes[idx]
        return candidatos[0]                               # inalcançável (candidatos ⊆ passes)

    def _checar_host_e_credencial(self, meta, spec):
        """Última linha de defesa do host e da credencial (LEVANTA RuntimeError => nada é
        spawnado nem travado; a passada escala). Duas regras, ambas só para os specs que
        as declaram (as plataformas vivas não declaram => nada muda para elas):

          1. HOST -> PLATAFORMA: o host do curso está em `hosts` de OUTRA plataforma =>
             recusa. O caso real: uma entrada Cademí/Entrega Digital sem a linha
             `plataforma:` vira "hotmart" no carregador e rodaria o motor Hotmart no
             `.chrome-profile` VIVO, na conta errada.
          2. CREDENCIAL DO TENANT (`sessao_por_tenant`): sem `session_path` => recusa (o
             default do motor pode ser um arquivo ÚNICO para todos os tenants); com um
             `session_path` que outro curso do YAML usa com OUTRA conta ou OUTRO host =>
             recusa (dois tenants/contas no mesmo storage_state misturam credencial e um
             sobrescreve a renovação do outro). Caminhos comparados só por STRING
             (normpath relativo ao cwd do motor de cada curso) — o arquivo de sessão NUNCA
             é aberto nem sondado aqui.
        As mensagens passam a URL/caminho pelo `rotulo_seguro`: texto do daemon nunca casa
        as âncoras de morte do classificador."""
        from maestro.rotulo import rotulo_seguro
        for plat, outro in _PLATAFORMAS.items():
            if (plat != meta.plataforma and outro.hosts
                    and plataforma_suportada(meta.url, outro.hosts)):
                raise RuntimeError(
                    f"host de {plat} com plataforma {meta.plataforma!r} no YAML — "
                    f"fail-closed, não disparo {rotulo_seguro(meta.url, maximo=90)} "
                    f"(corrija a linha plataforma: da conta {meta.conta!r})")
        if not spec.sessao_por_tenant:
            return
        sess = str(getattr(meta, "session_path", "") or "")
        if not sess:
            raise RuntimeError(
                f"{meta.plataforma} exige session_path do tenant no YAML (conta "
                f"{meta.conta!r}) — sem ele o motor usaria o arquivo padrão, que não é do "
                f"tenant: fail-closed, não disparo {rotulo_seguro(meta.url, maximo=90)}")
        host = plataforma_de_url(meta.url)
        alvo = _caminho_normalizado(self._motor_dir_de(meta.plataforma), sess)
        for outro in self._meta.values():
            outra_sess = str(getattr(outro, "session_path", "") or "")
            if outro is meta or not outra_sess:
                continue
            if _caminho_normalizado(self._motor_dir_de(outro.plataforma),
                                    outra_sess) != alvo:
                continue
            if outro.conta != meta.conta or plataforma_de_url(outro.url) != host:
                raise RuntimeError(
                    f"session_path da conta {meta.conta!r} também serve a conta "
                    f"{outro.conta!r} ({rotulo_seguro(plataforma_de_url(outro.url), maximo=60)})"
                    f" — credencial de um tenant não vale para outro: fail-closed, não "
                    f"disparo {rotulo_seguro(meta.url, maximo=90)}")

    def _montar(self, meta, passe="base"):
        spec = _PLATAFORMAS.get(meta.plataforma)
        if spec is None:
            # fail-closed: o motor só sabe as plataformas mapeadas. Uma nova nunca é
            # capturada às cegas (o gate de plataforma-nova já deveria tê-la barrado
            # antes; isto é a última linha de defesa).
            raise RuntimeError(
                f"plataforma {meta.plataforma!r} sem módulo de motor conhecido — "
                f"fail-closed, não capturo {meta.url}")
        self._checar_host_e_credencial(meta, spec)        # fail-closed ANTES de montar env
        motor_dir = self._motor_dir_de(meta.plataforma)
        cmd = [self._motor_python, "-m", spec.modulo, meta.url]
        # FLAG DO PASSE escolhido (`_escolher_passe`): base=sem flag; audio=--audio;
        # embed=--embed; nao-video=--nao-video. Hotmart (motor.cli) tem os 4 passes; a
        # Stoa (motor.stoa) tem base/embed/nao-video (o CLI dela conhece --embed e
        # --nao-video, grupo mutuamente exclusivo, default áudio-nativo = base). Os
        # áudio-nativos restantes ficam em ("base",) => flag None => comando inalterado
        # (passar-lhes uma flag seria argumento desconhecido do módulo errado).
        flag = _PASSE_FLAG.get(passe)
        if flag:
            cmd.append(flag)
        env = dict(os.environ)
        # ORDEM (importa p/ o anti-ban): extra_env global PRIMEIRO (base overridável); a
        # config da PLATAFORMA depois (o perfil DEDICADO/URL vence um extra_env global); os
        # INVIOLÁVEIS por último (Groq/HEADLESS/MOTOR_BROWSER vencem tudo — o exato anti-ban
        # que este executor existe para cravar).
        env.update(self._extra_env)
        spec_env = dict(spec.env)
        for k, v in spec.env:                              # perfil dedicado, LESSON_TIMEOUT_S
            env[k] = v
        # PERFIL DEDICADO POR CONTA (fix da causa-raiz do FLAP — ProcessSingleton):
        # `make_context` faz `launch_persistent_context(CHROME_USER_DATA_DIR)`, e o Chrome
        # cria um SingletonLock POR user-data-dir — só UM processo por diretório de perfil.
        # Sem esta linha, Hotmart e os 3 Memberkit (channel=chrome) caíam TODOS no default
        # `.chrome-profile` e, rodando em PARALELO sob o daemon (contas distintas), o 2º+ a
        # subir batia em "Failed to create a ProcessSingleton" e MORRIA na largada = o flap.
        # Damos a CADA conta seu próprio perfil (o storage_state/sessão é injetado à parte,
        # de `.hotmart-session.json`/MEMBERKIT_SESSION_PATH — trocar o perfil NÃO perde o
        # login). Plataformas cujo spec JÁ define um perfil (Stoa/Kajabi, Chromium isolado,
        # conta única) são mantidas intactas — não colidem e têm sessão viva nesse perfil.
        # FORÇA (não setdefault): um CHROME_USER_DATA_DIR herdado do ambiente do daemon
        # NÃO pode fazer duas contas partilharem perfil (seria o mesmo ban por trás).
        if "CHROME_USER_DATA_DIR" not in spec_env:
            env["CHROME_USER_DATA_DIR"] = _perfil_de_conta(meta.conta)
        if spec.url_env:                                   # STOA_URL / KAJABI_URL = meta.url
            env[spec.url_env] = meta.url
        # SESSÃO EXISTENTE (INVIOLÁVEL anti-login): aponta o storage_state que o usuário
        # semeou por login manual. Só quando o spec declara `session_env` E o curso traz
        # `session_path` — plataformas antigas (session_env='') ficam INTACTAS, e um
        # session_path vazio deixa o motor no default dele (cwd) em vez de inventar
        # caminho. O motor NUNCA loga sozinho: sessão morta = SessionDeadError (abort).
        if spec.session_env and getattr(meta, "session_path", ""):
            env[spec.session_env] = meta.session_path
        # TENANT por-curso (white-label — Curseduca): aplicado DEPOIS do spec.env de
        # propósito, para que o `tenant:` do YAML VENÇA o default fixado no spec (o
        # caminho multi-tenant sem tocar código). Só quando o spec declara `tenant_env`
        # E o curso traz `tenant` — nas demais plataformas (tenant_env='') é inerte.
        if spec.tenant_env and getattr(meta, "tenant", ""):
            env[spec.tenant_env] = meta.tenant
        # PYTHONPATH = a árvore do motor DESTA plataforma, p/ o `python -m <modulo>`
        # resolver o pacote certo (ex.: Stoa vive no worktree, não em /aula). Sobrepõe
        # o PYTHONPATH herdado do daemon (que aponta pra árvore da Athena, sem `motor`).
        env["PYTHONPATH"] = motor_dir
        env["WHISPER_BACKEND"] = "groq"                    # INVIOLÁVEL Groq (vence extra_env)
        if self._groq_key:
            env["GROQ_API_KEY"] = self._groq_key
        # ISOLAMENTO DE NAVEGADOR (anti-ban): Stoa/Kajabi no Chromium EMBUTIDO (não colide
        # com o Chrome do sistema do Hotmart no singleton do macOS); Hotmart/Memberkit
        # seguem channel=chrome. Popar quando não-chromium GARANTE channel=chrome mesmo que
        # um MOTOR_BROWSER tenha vazado do ambiente — o Hotmart NUNCA sai do channel=chrome.
        if spec.chromium:
            env["MOTOR_BROWSER"] = "chromium"
        else:
            env.pop("MOTOR_BROWSER", None)
        if spec.headless:
            env["HEADLESS"] = "1"                          # Memberkit (áudio-nativo, roda cego)
        else:
            env.pop("HEADLESS", None)                      # HEADED (sonda/áudio falham headless)
        return cmd, env, motor_dir


def _total_esperado(projeto, acesso, curso_url, total_esperado_fn):
    """Total ESPERADO de aulas do curso — o DENOMINADOR da régua de completude
    (no_notion >= total), a MESMA régua do gate I-1 (ProgressoNotion.completo) e da
    cabeça de captura. Fonte: `total_esperado_fn` injetado; senão, a ENUMERAÇÃO do
    tracker (count(estado_aulas) do course_id resolvido pela fila). Devolve 0 quando
    NÃO dá para determinar (sem course_id / exec falha).

    O 0 é FAIL-CLOSED de propósito: o chamador (guard anti-dup) NUNCA declara completo
    com total desconhecido — trata como RETOMAR (re-captura idempotente), nunca como
    'pular'. O lado seguro do erro é re-capturar, jamais abandonar um curso parcial
    declarando-o pronto (Inviolável 4).

    LIMITAÇÃO CONHECIDA (achado [6], resíduo documentado do Inviolável 4): o total vem
    da enumeração do PRÓPRIO tracker. Uma enumeração CURTA (ex.: 65 linhas para um curso
    de 537) faz total=65; o Notion com 65 então SATISFAZ 65>=65 e o guard pula — um
    falso-pronto por SUB-ENUMERAÇÃO que a checagem-contra-Notion, por construção, não
    consegue pegar (o numerador é verificado no Notion, mas o denominador ainda é o
    tracker). Não há, na arquitetura atual, um oráculo INDEPENDENTE de quantas aulas o
    curso tem na plataforma; inventar um número seria pior que a limitação. O
    fail-closed cobre o total DESCONHECIDO (0 -> retoma); a sub-enumeração silenciosa
    (total presente porém baixo) fica como limitação registrada até existir uma
    enumeração independente da plataforma para servir de denominador."""
    if total_esperado_fn is not None:
        try:
            return max(int(total_esperado_fn(curso_url)), 0)
        except Exception:
            return 0
    try:
        course_id = resolver_course_id(projeto, acesso, curso_url)
        if not course_id:
            return 0
        total, _done, _pend = progresso(projeto, acesso, course_id)
        return total
    except Exception:
        return 0


def coordenar(projeto, acesso, voz, *, executor, curso_url, estado, agora=None,
              ja_no_notion=None, progresso_notion_fn=None, total_esperado_fn=None,
              sintese_fn=None):
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
      - ANTI-DUPLICIDADE POR COMPLETUDE (não por presença): só pula um curso que o
        Notion (fonte da verdade durável) prova COMPLETO (no_notion >= total_esperado,
        a régua do gate I-1); um curso PARCIAL é RETOMADO, nunca declarado pronto —
        resiliente a restart (checa a cada ciclo);
      - COMPLETUDE POR NOTION, NÃO POR FLAG (gate I-1): quando o tracker diz 'done',
        a conclusão só é DECLARADA se o Auditor CONFIRMAR contra o Notion; falso-pronto
        é REJEITADO e escalado, e o curso NÃO é marcado CONCLUIDO (nem ingerido).

    `ja_no_notion`: seam injetável (curso_url)->(tem_aulas, qtd). Default = checar o
    Notion real via `curso_ja_no_notion` (exec_app no container do app). Injetável
    para os testes passarem um dublê sem tocar Notion/docker. `qtd` (no_notion) é o
    NUMERADOR da régua de completude do guard anti-dup.

    `total_esperado_fn`: seam injetável (curso_url)->int — o DENOMINADOR (total esperado)
    do guard anti-dup. Default = enumeração do tracker (`_total_esperado`). 0/incerto =>
    fail-closed (retoma, nunca conclui por presença).

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
            _tem, qtd = ja_no_notion(curso_url)
        except Exception as e:
            # Não deu para confirmar: NÃO enfileira às cegas (evita re-captura) e
            # ESCALA honesto; a fase segue NOVO -> o próximo ciclo re-tenta.
            pedido = (f"[{projeto.nome}] NÃO consegui verificar no Notion se {curso_url} "
                      f"já foi capturado (anti-duplicidade): {str(e)[:140]} — NÃO "
                      f"enfileiro (evito re-captura às cegas); re-tento no próximo ciclo")
            voz.escalar(Problema("antidup_notion_inacessivel", curso_url, pedido, "aviso"),
                        pedido)
            return Acao("", False, True, pedido)
        # PRESENÇA (qtd>0) NÃO É COMPLETUDE. Só pula (marca CONCLUIDO) quando o Notion
        # PROVA completude: no_notion >= total_esperado (a MESMA régua do gate I-1 /
        # ProgressoNotion.completo). O bug que isto mata: um curso interrompido em
        # 200/537 (Mac-off / restart do Maestro) tinha qtd=200>0 e era declarado
        # CONCLUIDO por PRESENÇA — falso-pronto que NUNCA re-enfileirava e abandonava
        # 337 aulas (viola Inviolável 4). Agora:
        #   - qtd == 0            -> curso NOVO: segue para a captura (FASE 1/2);
        #   - qtd >= total (>0)   -> COMPLETO provado no Notion: pula (CONCLUIDO);
        #   - 0 < qtd < total, ou total DESCONHECIDO -> PARCIAL/incerto: RETOMA (cai na
        #     captura, enfileiramento idempotente + gate I-1), JAMAIS conclui por presença.
        # Fail-closed: sem total confiável, o lado seguro é re-capturar, nunca abandonar.
        if qtd > 0:
            total_esp = _total_esperado(projeto, acesso, curso_url, total_esperado_fn)
            if ProgressoNotion(curso_url, qtd, agora).completo(total_esp):
                # COMPLETO comprovado (no_notion >= total): pula. Marca CONCLUIDO só para
                # não re-reportar a cada ciclo DESTE processo — a decisão é durável
                # (Notion + enumeração), então pós-restart a fase volta a NOVO e o skip
                # é re-decidido do zero pela MESMA prova.
                estado["fase"] = FASE_CONCLUIDO
                acao = Acao(f"[{projeto.nome}] {curso_url} JÁ capturado e COMPLETO "
                            f"({qtd}/{total_esp} aulas no Notion) — pulo o enfileiramento "
                            f"(anti-duplicidade)", True, False)
                voz.avisar_acao(acao)
                return acao
            # PARCIAL ou total incerto: NÃO conclui por presença. Reporta a RETOMADA
            # uma vez (latch por-curso, não spamma) e SEGUE para a captura (retomada
            # idempotente). O gate I-1 na FASE_CAPTURANDO só declarará pronto quando o
            # Notion alcançar o total.
            if not estado.get("retomada_avisada"):
                denom = total_esp if total_esp > 0 else "?"
                acao_r = Acao(f"[{projeto.nome}] {curso_url} PARCIAL no Notion "
                              f"({qtd}/{denom}) — NÃO declaro pronto por presença; "
                              f"RETOMO a captura (anti-falso-pronto, Inviolável 4)",
                              True, False)
                voz.avisar_acao(acao_r)
                estado["retomada_avisada"] = True

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
            # SEM voz aqui: o Auditor só emite o laudo. A ESCALAÇÃO é nossa, com LATCH
            # por-curso — senão um curso preso (ex.: aulas 'falhou' por parede anti-ban,
            # terminais, que zeram `pend` mas nunca chegam ao Notion) re-escalaria um
            # 'falso_pronto' CRÍTICO a cada ciclo (120s), inundando o Telegram e
            # dessensibilizando o operador à categoria crítica. Espelha o latch
            # `parede_reportada` da cabeça de captura: reporta UMA vez por episódio.
            laudo = auditor.auditar_conclusao(curso_url, prog_notion, total)
            if not laudo.aprovado:
                # FALSO-PRONTO: NÃO ingere, NÃO marca CONCLUIDO — a fase segue CAPTURANDO
                # (re-tenta quando o Notion alcançar `total`).
                falta = max(total - prog_notion.no_notion, 0)
                if not estado.get("falso_pronto_reportado"):
                    pedido = (f"[{projeto.nome}] {curso_url} reportado PRONTO pelo tracker "
                              f"mas o Notion tem {prog_notion.no_notion}/{total} — {falta} "
                              f"faltando (falso-pronto REJEITADO, I-1); NÃO declaro concluído")
                    voz.escalar(Problema("falso_pronto", curso_url, pedido, "critico"), pedido)
                    estado["falso_pronto_reportado"] = True
                return Acao("", False, True,
                            f"[{projeto.nome}] conclusão de {curso_url} REJEITADA pelo Auditor "
                            f"(Notion tem {prog_notion.no_notion}/{total} — {falta} faltando)")
            # aprovado: o Notion alcançou o total -> destrava o latch (novo episódio
            # futuro poderá re-escalar honesto).
            estado["falso_pronto_reportado"] = False

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
        # ESTEIRA pós-captura: dispara a Capacidade C (Sintetizador) SÓ agora, depois do
        # auto-ingest confirmado (o Sintetizador consome o que ficou no pgvector).
        _hooks_esteira(projeto, curso_url, estado, sintese_fn=sintese_fn)
        return acao

    return None


def _hooks_esteira(projeto, curso_url, estado, *, sintese_fn=None) -> list:
    """Esteira downstream — ponto de extensão APÓS o auto-ingest confirmado (nunca
    antes: o Sintetizador precisa das aulas já ingeridas no pgvector).

    Capacidade C (Sintetizador) entra aqui via o seam `sintese_fn(projeto, curso_url)`:
    a Athena classifica o curso (how-to?), aciona o Sintetizador e faz o artefato PASSAR
    pelo Portão POP (Capacidade A) antes de registrar — toda a lógica vive em
    `sintetizador_orq.orquestrar_sintese`; aqui só a DISPARAMOS no gatilho certo (pós-
    captura). `sintese_fn` None => NO-OP honesto (retrocompatível; ligado só quando o
    Sintetizador real estiver plugado).

    ISOLAMENTO: a esteira NUNCA pode derrubar a captura. Uma falha do Sintetizador é
    registrada no `estado` (auditoria) e engolida aqui — a captura do curso já está
    concluída e confirmada no Notion; a síntese é um passo POSTERIOR e independente.
    """
    if sintese_fn is None:
        return []
    try:
        estado["sintese"] = sintese_fn(projeto, curso_url)
    except Exception as e:                    # esteira NUNCA derruba a captura
        estado["sintese_erro"] = str(e)[:200]
    return []
