"""Adaptador do projeto 'conhecimento': checks específicos da fila de captura
(job travado / falhou) e ações (reaper/reenqueue), lendo o Postgres do projeto.
Fora do núcleo — o Maestro genérico não sabe o que é 'captura'."""
import time

from maestro.sentinela import Problema
from maestro.playbook import Acao

TETO_JOB_S = 4 * 3600.0


def _conn(database_url):
    import psycopg
    return psycopg.connect(database_url, autocommit=True, connect_timeout=5)


def checar(projeto) -> list:
    """Problemas específicos da fila do conhecimento."""
    ps = []
    try:
        with _conn(projeto.database_url) as c:
            rows = c.execute("SELECT id, status, extract(epoch from claimed_at), error "
                             "FROM fila_captura WHERE status IN ('capturando','falhou')").fetchall()
    except Exception:
        return ps  # sem acesso ao banco -> sem checks (o núcleo já cobre serviço down)
    agora = time.time()
    for jid, status, claimed_epoch, error in rows:
        if status == "capturando" and (agora - (claimed_epoch or 0.0)) > TETO_JOB_S:
            ps.append(Problema("job_travado", str(jid), "captura travada", "critico"))
        elif status == "falhou":
            ps.append(Problema("job_falhou", str(jid), (error or "")[:200], "aviso"))
    return ps


def resolver(problema, acesso, projeto) -> Acao:
    if problema.tipo == "job_travado":
        acesso.restart("worker")
        try:
            with _conn(projeto.database_url) as c:
                c.execute("UPDATE fila_captura SET status='enfileirado', claimed_by=NULL, "
                          "claimed_at=NULL WHERE id=%s AND status='capturando'", (int(problema.alvo),))
        except Exception:
            pass
        return Acao(f"[{projeto.nome}] job {problema.alvo} travado — reiniciei o worker e re-enfileirei",
                    True, False)
    if problema.tipo == "job_falhou":
        return Acao("", False, True, f"[{projeto.nome}] job {problema.alvo} falhou: {problema.detalhe}")
    return Acao("", False, True, f"[{projeto.nome}] {problema.tipo}")
