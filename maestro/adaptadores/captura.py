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
