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
import time

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


def coordenar(projeto, acesso, voz, *, executor, curso_url, estado, agora=None):
    """Coordena o PROTOCOLO DE CAPTURA de UM curso (identificado por `curso_url`), um
    passo por ciclo, avançando a máquina de estados em `estado` (dict por curso,
    mutável, persiste entre ciclos). Dirige a `voz` diretamente (pede reseed / avisa /
    escala honesto) e devolve a Acao do passo (ou None quando não há nada a reportar).
    O loop apenas registra a Acao — não re-dirige a voz (o coordenador é dono do seu
    reporte).

    INVIOLÁVEIS cravados aqui:
      - sessão morta -> NÃO dispara captura; pede reseed (nunca loga sozinho);
      - captura SEMPRE via `executor` residencial (nunca Chrome na VPS — anti-ban);
      - nenhuma etapa é dada como sucesso sem confirmação (disparo, ingest).
    """
    agora = time.time() if agora is None else agora
    fase = estado.get("fase", FASE_NOVO)

    if fase == FASE_CONCLUIDO:
        return None                                       # idempotente: nada a fazer

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
