import time

from maestro.adaptadores import conhecimento
from maestro.sentinela import Problema
from maestro.registro import Projeto


def _proj():
    return Projeto("c", "cp", ("worker",), {}, "conhecimento",
                   db_container="cp_db", db_name="conhecimento", db_user="postgres")


class FakeAcesso:
    """Dublê que modela o mecanismo real: exec_sql recebe (container, sql, db, user)
    e devolve linhas -tA (campos por '|'); restart e UPDATEs ficam registrados.
    checar() roda DUAS queries de leitura — a da fila (fila_captura status IN
    capturando/falhou) e a de captura vazia (JOIN com estado_aulas, status='pronto').
    O dublê roteia por conteúdo do SQL, senão a query de vazia leria linhas de fila."""
    def __init__(self, linhas, vazias=None):
        self._linhas = linhas
        self._vazias = vazias or []
        self.chamadas = []
        self.reiniciados = []
        self.updates = []

    def exec_sql(self, container, sql, *, db, user="postgres", rows=True):
        self.chamadas.append((container, sql, db, user, rows))
        if sql.strip().upper().startswith("UPDATE"):
            self.updates.append(sql)
            return []
        if "estado_aulas" in sql:            # query de captura vazia
            return list(self._vazias)
        return list(self._linhas)            # query da fila

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


def test_checar_detecta_captura_vazia_falso_sucesso():
    # curso concluído (fila 'pronto') mas capturou 0 de N aulas = falso-sucesso.
    # É EXATAMENTE o piloto que passou batido: total=3, done=0 (tudo nao_capturavel).
    ac = FakeAcesso([], vazias=["1978824|3|0"])
    ps = conhecimento.checar(_proj(), ac)
    assert len(ps) == 1 and ps[0].tipo == "captura_vazia"
    assert ps[0].alvo == "1978824"
    assert "0 de 3" in ps[0].detalhe and "falso-sucesso" in ps[0].detalhe
    # provou que a detecção junta fila 'pronto' com estado_aulas via exec_sql:
    sqls = [c[1] for c in ac.chamadas]
    assert any("estado_aulas" in s and "'pronto'" in s for s in sqls)


def test_checar_captura_com_conteudo_nao_e_vazia():
    # a query de vazia só devolve cursos done=0 (HAVING no SQL real); se ela não
    # devolve nada, não há problema — curso que capturou algo não escala.
    ps = conhecimento.checar(_proj(), FakeAcesso([], vazias=[]))
    assert ps == []


def test_resolver_captura_vazia_escala_nao_age():
    ac = FakeAcesso([])
    r = conhecimento.resolver(
        Problema("captura_vazia", "1978824",
                 "curso 1978824 concluído mas capturou 0 de 3 aulas — provável falso-sucesso",
                 "critico"),
        ac, _proj())
    assert r.escalar and not r.executada
    assert ac.reiniciados == [] and ac.updates == []   # NÃO age sozinho
    assert "falso-sucesso" in r.pedido


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
