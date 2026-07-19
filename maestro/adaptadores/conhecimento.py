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

# 'done' = sucesso terminal REAL (contrato de painel/estado.py: no_notion /
# anexos_baixados). Um curso com fila 'pronto' mas done=0 e total>0 CAPTUROU NADA —
# falso-sucesso. O núcleo/Sentinela não pega isso (job saiu 'pronto', serviço no ar);
# foi o buraco que deixou o piloto (curso 1978824, total=3 done=0) passar batido.
# JOIN interno com estado_aulas: fila 'pronto' sem course_id/sem aulas não casa (não
# vira falso-positivo); HAVING garante total>0 e done=0 do lado do banco.
_SQL_VAZIA = (
    "SELECT f.course_id, count(ea.hash), "
    "count(ea.hash) FILTER (WHERE ea.status IN ('no_notion','anexos_baixados')) "
    "FROM fila_captura f JOIN estado_aulas ea ON ea.course_id = f.course_id "
    "WHERE f.status = 'pronto' "
    "GROUP BY f.course_id "
    "HAVING count(ea.hash) > 0 "
    "AND count(ea.hash) FILTER (WHERE ea.status IN ('no_notion','anexos_baixados')) = 0")


def checar(projeto, acesso) -> list:
    """Problemas específicos da fila do conhecimento, lidos via exec_sql."""
    ps = []
    if not getattr(projeto, "db_container", ""):
        return ps
    try:
        linhas = acesso.exec_sql(projeto.db_container, _SQL_FILA,
                                 db=projeto.db_name, user=projeto.db_user)
        vazias = acesso.exec_sql(projeto.db_container, _SQL_VAZIA,
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
    for ln in vazias:
        parts = ln.split("|", 2)
        if len(parts) < 3:
            continue
        cid, total_s, done_s = parts
        try:
            total, done = int(total_s), int(done_s)
        except ValueError:
            continue
        if total > 0 and done == 0:   # o HAVING já garante; re-checa por defesa
            ps.append(Problema(
                "captura_vazia", cid,
                f"curso {cid} concluído mas capturou 0 de {total} aulas — "
                f"provável falso-sucesso", "critico"))
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
    if problema.tipo == "captura_vazia":
        # falso-sucesso é diagnóstico, não conserto automático: re-enfileirar às cegas
        # só re-queima a conta contra o mesmo gate (IP datacenter/anti-ban). ESCALA.
        return Acao("", False, True, f"[{projeto.nome}] {problema.detalhe}")
    return Acao("", False, True, f"[{projeto.nome}] {problema.tipo}")
