import time

from maestro.adaptadores import conhecimento
from maestro.sentinela import Problema
from maestro.registro import Projeto


def _proj():
    return Projeto("c", "cp", ("worker",), {}, "conhecimento",
                   db_container="cp_db", db_name="conhecimento", db_user="postgres")


class FakeAcesso:
    """Dublê que modela o mecanismo real: exec_sql recebe (container, sql, db, user)
    e devolve linhas -tA (campos por '|'); restart e UPDATEs ficam registrados."""
    def __init__(self, linhas):
        self._linhas = linhas
        self.chamadas = []
        self.reiniciados = []
        self.updates = []

    def exec_sql(self, container, sql, *, db, user="postgres", rows=True):
        self.chamadas.append((container, sql, db, user, rows))
        if sql.strip().upper().startswith("UPDATE"):
            self.updates.append(sql)
            return []
        return list(self._linhas)

    def restart(self, nome):
        self.reiniciados.append(nome)


def test_checar_detecta_job_travado_via_exec_sql():
    velho = time.time() - (5 * 3600)   # > TETO_JOB_S (4h)
    ac = FakeAcesso([f"7|capturando|{velho}|"])
    ps = conhecimento.checar(_proj(), ac)
    assert len(ps) == 1 and ps[0].tipo == "job_travado" and ps[0].alvo == "7"
    # provou que foi pelo container certo, não por DNS interno:
    cont, sql, db, user, rows = ac.chamadas[0]
    assert cont == "cp_db" and db == "conhecimento" and "fila_captura" in sql


def test_checar_job_recente_nao_e_travado():
    recente = time.time() - 60
    ps = conhecimento.checar(_proj(), FakeAcesso([f"9|capturando|{recente}|"]))
    assert ps == []


def test_checar_job_falhou_vira_aviso():
    ps = conhecimento.checar(_proj(), FakeAcesso(["3|falhou|0|deu ruim"]))
    assert len(ps) == 1 and ps[0].tipo == "job_falhou" and ps[0].detalhe == "deu ruim"


def test_checar_sem_db_container_retorna_vazio():
    p = Projeto("c", "cp", ("worker",), {}, "conhecimento")  # db_container=""
    assert conhecimento.checar(p, FakeAcesso(["1|capturando|0|"])) == []


def test_checar_exec_falha_escala_nao_silencia():
    # falha de acesso ao banco NÃO pode virar "fila limpa" — tem que SINALIZAR.
    class Boom:
        def exec_sql(self, *a, **k): raise RuntimeError("socket off")
    ps = conhecimento.checar(_proj(), Boom())
    assert len(ps) == 1 and ps[0].tipo == "conhecimento_db_inacessivel"


def test_resolver_db_inacessivel_escala():
    r = conhecimento.resolver(
        Problema("conhecimento_db_inacessivel", "cp_db", "socket off", "aviso"),
        FakeAcesso([]), _proj())
    assert r.escalar and not r.executada


def test_resolver_travado_reinicia_worker_e_reenfileira():
    ac = FakeAcesso([])
    r = conhecimento.resolver(Problema("job_travado", "7", "x", "critico"), ac, _proj())
    assert r.executada and not r.escalar
    assert ac.reiniciados == ["worker"]
    assert ac.updates and "id=7" in ac.updates[0] and "enfileirado" in ac.updates[0]


def test_resolver_job_falhou_escala():
    r = conhecimento.resolver(Problema("job_falhou", "3", "erro", "aviso"), FakeAcesso([]), _proj())
    assert r.escalar and not r.executada


def test_resolver_travado_update_falha_reporta_honesto():
    # se o re-enfileiramento falhar, NÃO pode dizer "re-enfileirei".
    class AcessoUpdateFalha:
        def __init__(self): self.reiniciados=[]
        def restart(self, n): self.reiniciados.append(n)
        def exec_sql(self, *a, **k): raise RuntimeError("update off")
    ac = AcessoUpdateFalha()
    r = conhecimento.resolver(Problema("job_travado","7","x","critico"), ac, _proj())
    assert ac.reiniciados == ["worker"]        # reiniciou de fato
    assert r.escalar and not r.executada       # mas escala honesto
    assert "FALHEI ao re-enfileirar" in r.pedido
