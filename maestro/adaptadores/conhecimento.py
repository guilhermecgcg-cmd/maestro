"""Adaptador do projeto 'conhecimento': checks específicos da fila de captura
(job travado / falhou) e ações (reaper/reenqueue). Alcança o Postgres do projeto
via `docker exec ... psql` pelo socket (Acesso.exec_sql) — o Maestro roda num
projeto Easypanel PRÓPRIO, isolado da rede do conhecimento, então NÃO conecta por
DNS interno (`db:5432`); entra de dentro do container do banco. Fora do núcleo — o
Maestro genérico não sabe o que é 'captura'."""
import time

from maestro.sentinela import Problema
from maestro.playbook import Acao

TETO_JOB_S = 4 * 3600.0

_SQL_FILA = (
    "SELECT id, status, extract(epoch from claimed_at), "
    "replace(replace(replace(coalesce(error,''),chr(10),' '),chr(13),' '),'|','/') "
    "FROM fila_captura WHERE status IN ('capturando','falhou')")


def checar(projeto, acesso) -> list:
    """Problemas específicos da fila do conhecimento, lidos via exec_sql."""
    ps = []
    if not getattr(projeto, "db_container", ""):
        return ps
    try:
        linhas = acesso.exec_sql(projeto.db_container, _SQL_FILA,
                                 db=projeto.db_name, user=projeto.db_user)
    except Exception as e:
        # NÃO silenciar: exec_sql que falha (container/db/psql errados) é
        # indistinguível de "fila limpa" — e fila sem vigilância = risco de queimar
        # conta paga. Emite um problema que ESCALA (o núcleo só cobre o banco CAÍDO,
        # não misconfig/auth/erro de query).
        return [Problema("conhecimento_db_inacessivel", projeto.db_container,
                         f"sem acesso ao banco via exec: {str(e)[:160]}", "aviso")]
    agora = time.time()
    for ln in linhas:
        parts = ln.split("|", 3)
        if len(parts) < 4:
            continue
        jid, status, epoch, error = parts
        try:
            claimed = float(epoch) if epoch else 0.0
        except ValueError:
            claimed = 0.0
        if status == "capturando" and (agora - claimed) > TETO_JOB_S:
            ps.append(Problema("job_travado", jid, "captura travada", "critico"))
        elif status == "falhou":
            ps.append(Problema("job_falhou", jid, error[:200], "aviso"))
    return ps


def resolver(problema, acesso, projeto) -> Acao:
    if problema.tipo == "job_travado":
        acesso.restart("worker")
        try:
            acesso.exec_sql(
                projeto.db_container,
                "UPDATE fila_captura SET status='enfileirado', claimed_by=NULL, "
                f"claimed_at=NULL WHERE id={int(problema.alvo)} AND status='capturando'",
                db=projeto.db_name, user=projeto.db_user, rows=False)
        except Exception as e:
            # não mentir "re-enfileirei" se o UPDATE falhou: reiniciei o worker, mas
            # o re-enfileiramento não foi confirmado -> escala pra decisão humana.
            return Acao("", False, True,
                        f"[{projeto.nome}] job {problema.alvo}: reiniciei o worker mas "
                        f"FALHEI ao re-enfileirar: {str(e)[:120]}")
        return Acao(f"[{projeto.nome}] job {problema.alvo} travado — reiniciei o worker e re-enfileirei",
                    True, False)
    if problema.tipo == "conhecimento_db_inacessivel":
        return Acao("", False, True,
                    f"[{projeto.nome}] NÃO consigo vigiar a fila: {problema.detalhe}")
    if problema.tipo == "job_falhou":
        return Acao("", False, True, f"[{projeto.nome}] job {problema.alvo} falhou: {problema.detalhe}")
    return Acao("", False, True, f"[{projeto.nome}] {problema.tipo}")
