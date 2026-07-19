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
"""
import time

from maestro.adaptadores import conhecimento
from maestro.playbook import Acao
from maestro.sentinela import Problema

# Sessão da plataforma é durável mas perecível (TGC é session cookie; storage_state
# é re-semeado periodicamente pelo Mac). Acima deste teto tratamos como morta e
# PEDIMOS reseed — melhor pedir cedo do que disparar captura contra sessão expirada
# (parede/erro, que I-3 proíbe tratar como sucesso).
SESSAO_MAX_IDADE_S = 6 * 3600.0

# Fases do protocolo de captura de UM curso. Persistem em `estado` entre ciclos do
# loop (mesmo padrão do `ultimo` de reconciliar): o coordenador é chamado a cada
# ciclo e avança a máquina de estados sem bloquear.
FASE_NOVO = "novo"                # ainda não disparado
FASE_CAPTURANDO = "capturando"    # disparado no residencial; monitorando progresso
FASE_CONCLUIDO = "concluido"      # capturado + auto-ingest confirmado

# --- QUERIES DO TRACKER -----------------------------------------------------
# ATENÇÃO: `sessao_plataforma` é o contrato ASSUMIDO do tracker de sessão (o motor
# de login grava validade + carimbo). VERIFICAR tabela/colunas reais antes de ativar
# (mesmo aviso do app_container em projetos.yaml). O MECANISMO — flag de validade +
# frescor por tempo -> viva/morta — não muda com o nome real da coluna.
_SQL_SESSAO = (
    "SELECT valida, extract(epoch from atualizada_em) "
    "FROM sessao_plataforma ORDER BY atualizada_em DESC LIMIT 1")

# Progresso reusa a tabela REAL do conhecimento (estado_aulas) e o mesmo contrato de
# 'done' terminal (no_notion/anexos_baixados) do adaptador irmão — não inventa schema.
_SQL_PROGRESSO = (
    "SELECT count(hash), "
    "count(hash) FILTER (WHERE status IN ('no_notion','anexos_baixados')) "
    "FROM estado_aulas WHERE course_id = {curso}")


def estado_sessao(projeto, acesso, *, agora) -> str:
    """'viva' | 'morta' | 'desconhecida'. NUNCA loga; só LÊ o tracker.

    'desconhecida' (exec falhou / sem linha) é distinto de 'morta' de propósito: um
    erro de acesso não pode virar 'viva' (dispararia captura sem sessão confirmada)
    nem 'morta' (pediria reseed à toa) — escala pra verificação, não age às cegas.
    """
    if not getattr(projeto, "db_container", ""):
        return "desconhecida"
    try:
        linhas = acesso.exec_sql(projeto.db_container, _SQL_SESSAO,
                                 db=projeto.db_name, user=projeto.db_user)
    except Exception:
        return "desconhecida"
    if not linhas:
        return "desconhecida"
    parts = linhas[0].split("|", 1)
    if len(parts) < 2:
        return "desconhecida"
    valida_s, epoch_s = parts
    if valida_s.strip().lower() in ("f", "false", "0", ""):
        return "morta"
    try:
        atualizada = float(epoch_s)
    except ValueError:
        return "desconhecida"
    if agora - atualizada > SESSAO_MAX_IDADE_S:
        return "morta"
    return "viva"


def progresso(projeto, acesso, curso) -> tuple:
    """(total, done, pendentes) do curso, lido do tracker (estado_aulas)."""
    linhas = acesso.exec_sql(
        projeto.db_container, _SQL_PROGRESSO.format(curso=int(curso)),
        db=projeto.db_name, user=projeto.db_user)
    if not linhas:
        return (0, 0, 0)
    parts = linhas[0].split("|", 1)
    try:
        total, done = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return (0, 0, 0)
    return (total, done, max(total - done, 0))


class FilaExecutor:
    """Executor residencial PADRÃO: ENFILEIRA a captura em fila_captura via exec_sql.

    Enfileirar é VPS-safe (é só um INSERT no Postgres, não abre Chrome). Quem executa
    o browser é o worker RESIDENCIAL no Mac, que reivindica a fila — respeitando o
    inviolável de que a captura por browser nunca roda no datacenter. Executores
    alternativos (ex.: callable Mac-side por outro canal) são injetáveis do mesmo jeito;
    o contrato é: `disparar(curso) -> confirmação truthy`, ou LEVANTA em falha.
    """
    def __init__(self, acesso, projeto):
        self._acesso = acesso
        self._projeto = projeto

    def disparar(self, curso):
        # ON CONFLICT: re-disparar um curso já na fila não duplica (idempotente).
        sql = ("INSERT INTO fila_captura (course_id, status) "
               f"VALUES ({int(curso)}, 'enfileirado') "
               "ON CONFLICT (course_id) DO UPDATE SET status='enfileirado'")
        self._acesso.exec_sql(self._projeto.db_container, sql,
                              db=self._projeto.db_name, user=self._projeto.db_user,
                              rows=False)
        return f"enfileirado:{int(curso)}"

    # SEAM DE EVOLUÇÃO (roadmap, NÃO construir agora): hoje o browser roda no worker
    # RESIDENCIAL do Mac (IP residencial), que só liga quando o Mac está ligado. Para
    # captura 24/7 com o Mac desligado, trocar o executor por um que roteie o browser
    # por um PROXY residencial a partir da VPS — mantendo o inviolável anti-ban (IP
    # residencial), sem depender do Mac. É só outro executor injetado: o coordenador
    # não muda.


def coordenar(projeto, acesso, voz, *, executor, curso, estado, agora=None):
    """Coordena o PROTOCOLO DE CAPTURA de UM curso, um passo por ciclo, avançando a
    máquina de estados em `estado` (dict por curso, mutável, persiste entre ciclos).
    Dirige a `voz` diretamente (pede reseed / avisa / escala honesto) e devolve a
    Acao do passo (ou None quando não há nada a reportar). O loop apenas registra a
    Acao — não re-dirige a voz (o coordenador é dono do seu reporte).

    INVIOLÁVEIS cravados aqui:
      - sessão morta/desconhecida -> NÃO dispara captura; pede reseed / escala;
      - captura SEMPRE via `executor` residencial (nunca Chrome na VPS — anti-ban);
      - nenhuma etapa é dada como sucesso sem confirmação (reseed, disparo, ingest).
    """
    agora = time.time() if agora is None else agora
    fase = estado.get("fase", FASE_NOVO)

    if fase == FASE_CONCLUIDO:
        return None                                       # idempotente: nada a fazer

    # ---- FASE 1: checar a sessão ANTES de qualquer disparo -----------------
    if fase == FASE_NOVO:
        sess = estado_sessao(projeto, acesso, agora=agora)
        if sess == "morta":
            # INVIOLÁVEL: nunca loga sozinho. PEDE reseed humano via voz (Telegram).
            pedido = (f"[{projeto.nome}] sessão da plataforma MORTA — preciso de RESEED "
                      f"(re-semear o storage_state pelo Mac e me avisar); NÃO faço login "
                      f"sozinho e NÃO capturo o curso {curso} sem sessão viva")
            voz.escalar(Problema("sessao_morta", str(curso), pedido, "critico"), pedido)
            return Acao("", False, True, pedido)
        if sess != "viva":                                # "desconhecida": não age às cegas
            pedido = (f"[{projeto.nome}] não consegui CONFIRMAR a sessão (curso {curso}); "
                      f"não disparo captura sem confirmar — verificar o tracker de sessão")
            voz.escalar(Problema("sessao_desconhecida", str(curso), pedido, "aviso"), pedido)
            return Acao("", False, True, pedido)

        # ---- FASE 2: sessão viva -> dispara via EXECUTOR residencial --------
        # O Maestro roda na VPS (datacenter). Capturar por browser AQUI queimaria a
        # conta (anti-ban). Então NÃO abrimos Chrome: delegamos ao executor, que dispara
        # o worker RESIDENCIAL (Mac). Falha/silêncio do executor -> escala honesto.
        try:
            confirmacao = executor.disparar(curso)
        except Exception as e:
            pedido = (f"[{projeto.nome}] FALHEI ao disparar a captura residencial do curso "
                      f"{curso}: {str(e)[:160]}")
            voz.escalar(Problema("captura_disparo_falhou", str(curso), pedido, "critico"), pedido)
            return Acao("", False, True, pedido)
        if not confirmacao:
            pedido = (f"[{projeto.nome}] disparo da captura do curso {curso} SEM confirmação "
                      f"do executor residencial — não assumo sucesso")
            voz.escalar(Problema("captura_disparo_sem_confirmacao", str(curso), pedido, "critico"),
                        pedido)
            return Acao("", False, True, pedido)
        estado["fase"] = FASE_CAPTURANDO
        acao = Acao(f"[{projeto.nome}] captura do curso {curso} disparada no residencial: "
                    f"{confirmacao}", True, False)
        voz.avisar_acao(acao)
        return acao

    # ---- FASE 3: monitorar progresso via Acesso (tracker) ------------------
    if fase == FASE_CAPTURANDO:
        try:
            total, done, pend = progresso(projeto, acesso, curso)
        except Exception as e:
            pedido = (f"[{projeto.nome}] não consigo ler o progresso do curso {curso}: "
                      f"{str(e)[:140]}")
            voz.escalar(Problema("progresso_inacessivel", str(curso), pedido, "aviso"), pedido)
            return Acao("", False, True, pedido)
        if not (total > 0 and done >= total):
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
            pedido = (f"[{projeto.nome}] curso {curso} capturado mas SEM app_container para "
                      f"auto-ingest — verificar o registro do projeto")
            voz.escalar(Problema("ingest_sem_alvo", str(curso), pedido, "aviso"), pedido)
            return Acao("", False, True, pedido)
        if not acao.executada:
            # reconcile falhou / sem RECONCILE_OK -> NÃO marca concluído; escala honesto.
            voz.escalar(Problema("ingest_falhou", str(curso), acao.pedido, "aviso"), acao.pedido)
            return acao
        estado["fase"] = FASE_CONCLUIDO
        voz.avisar_acao(acao)
        # SEAM DA ESTEIRA (declarado, não implementado): classificador fino + Sintetizador.
        _hooks_esteira(projeto, curso, estado)
        return acao

    return None


def _hooks_esteira(projeto, curso, estado) -> list:
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
