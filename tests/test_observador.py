"""Camada 1 — Observabilidade (os olhos). Testes com dublês que modelam o
MECANISMO (store durável ordenado por ts), nunca a coreografia. O ponto central:
uma sonda de UM instante NÃO enxerga um serviço que subiu-e-caiu; a série temporal
persistida SIM. Os testes de 'dentes' provam que, se a série for ignorada (ou o
timestamp cair), o observador volta a mentir 'up' pra um serviço que já caiu."""
from maestro import observador
from maestro.acesso import Servico


# --- Dublês que modelam o MECANISMO ----------------------------------------
class FakeAcesso:
    """Modela as costuras que o observador REUSA (não abre conexão nova)."""
    def __init__(self, servicos, saude, recursos, fila):
        self._servicos = servicos
        self._saude = saude
        self._recursos = recursos
        self._fila = fila

    def servicos(self):
        return dict(self._servicos)

    def saude_http(self, alvos):
        return {n: self._saude.get(n, False) for n in alvos}

    def recursos(self):
        return dict(self._recursos)

    def fila(self):
        return list(self._fila)


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        if s.startswith("create table"):
            self.store["criada"] = self.store.get("criada", 0) + 1
            return FakeCursor([])
        if s.startswith("insert into observacoes"):
            ts, servico, up, health, detalhe = params
            self.store["rows"].append((float(ts), servico, up, health, detalhe))
            return FakeCursor([])
        if s.startswith("select") and "observacoes" in s:
            servico = params[0]
            got = [(ts, up, health) for (ts, sv, up, health, _) in self.store["rows"]
                   if sv == servico]
            got.sort(key=lambda r: r[0])  # ordena pela série (por ts)
            return FakeCursor(got)
        raise AssertionError("SQL não modelado pelo dublê: " + s)

    def commit(self):
        self.store["commits"] = self.store.get("commits", 0) + 1


class FakeDB:
    """Fábrica de conexão injetada (estilo psycopg3: `with db() as conn`)."""
    def __init__(self):
        self.store = {"rows": [], "commits": 0, "criada": 0}

    def __call__(self):
        return FakeConn(self.store)


def _acesso():
    return FakeAcesso(
        servicos={"worker": Servico("worker", up=True, restarting=False),
                  "painel-api": Servico("painel-api", up=True, restarting=False)},
        saude={"painel-api": False, "worker": True},
        recursos={"disco_pct": 42.0, "ram_pct": 30.0},
        fila=[{"status": "capturando", "n": 2}])


# --- coletar_estado: snapshot com timestamp em CADA campo -------------------
def test_coletar_estado_monta_snapshot_das_costuras():
    est = observador.coletar_estado(_acesso(), {"painel-api": "http://x/health",
                                                 "worker": "http://w/health"}, agora=1000.0)
    servs = {s.nome: s for s in est.servicos}
    assert servs["worker"].up is True and servs["worker"].health is True
    assert servs["painel-api"].up is True and servs["painel-api"].health is False
    assert est.recursos.disco_pct == 42.0
    assert est.fila.itens == [{"status": "capturando", "n": 2}]


def test_coletar_estado_carimba_ts_em_cada_campo():
    est = observador.coletar_estado(_acesso(), {"painel-api": "http://x/health"},
                                    agora=1000.0)
    assert est.ts == 1000.0
    assert est.recursos.ts == 1000.0
    assert est.fila.ts == 1000.0
    assert all(s.ts == 1000.0 for s in est.servicos)


# --- registrar: escreve em observacoes + cria tabela idempotente -----------
def test_registrar_cria_tabela_e_insere_por_servico():
    db = FakeDB()
    est = observador.coletar_estado(_acesso(), {"painel-api": "http://x/health"},
                                    agora=1000.0)
    observador.registrar(db, est)
    assert db.store["criada"] >= 1            # CREATE TABLE IF NOT EXISTS rodou
    nomes = {r[1] for r in db.store["rows"]}
    assert {"worker", "painel-api"} <= nomes  # uma linha por serviço
    assert all(r[0] == 1000.0 for r in db.store["rows"])  # ts em toda linha
    assert db.store["commits"] >= 1


def test_registrar_idempotente_roda_todo_ciclo():
    db = FakeDB()
    est = observador.coletar_estado(_acesso(), {}, agora=1000.0)
    observador.registrar(db, est)
    observador.registrar(db, est)             # seguro rodar de novo
    assert db.store["criada"] == 2            # CREATE IF NOT EXISTS em cada ciclo


# --- A CORREÇÃO DA CEGUEIRA, modelada --------------------------------------
def test_flapping_pos_queda_ve_estado_real_nao_o_t0():
    """t0: worker UP. t1 (5s depois): worker CAIU. Uma sonda de instante em t0
    diria 'up' pra sempre. A série persistida sabe que caiu."""
    db = FakeDB()
    acc_up = FakeAcesso({"worker": Servico("worker", up=True, restarting=False)},
                        {"worker": True}, {"disco_pct": 10, "ram_pct": 10}, [])
    acc_down = FakeAcesso({"worker": Servico("worker", up=False, restarting=False)},
                          {"worker": False}, {"disco_pct": 10, "ram_pct": 10}, [])
    observador.registrar(db, observador.coletar_estado(acc_up, {}, agora=1000.0))
    observador.registrar(db, observador.coletar_estado(acc_down, {}, agora=1005.0))

    # flapping: mudou de estado dentro da janela -> True (subiu e caiu)
    assert observador.flapping(db, "worker", janela_s=60.0, agora=1005.0) is True
    # estado real pós-queda: NUNCA o "up" obsoleto do t0
    assert observador.estado_estavel(db, "worker") != "up"


def test_estado_estavel_quando_de_fato_estavel():
    db = FakeDB()
    acc = FakeAcesso({"worker": Servico("worker", up=True, restarting=False)},
                     {"worker": True}, {"disco_pct": 10, "ram_pct": 10}, [])
    observador.registrar(db, observador.coletar_estado(acc, {}, agora=1000.0))
    observador.registrar(db, observador.coletar_estado(acc, {}, agora=1005.0))
    assert observador.estado_estavel(db, "worker") == "up"
    assert observador.flapping(db, "worker", janela_s=60.0, agora=1005.0) is False


def test_flapping_ignora_amostras_fora_da_janela():
    db = FakeDB()
    acc_up = FakeAcesso({"worker": Servico("worker", up=True, restarting=False)},
                        {"worker": True}, {"disco_pct": 10, "ram_pct": 10}, [])
    acc_down = FakeAcesso({"worker": Servico("worker", up=False, restarting=False)},
                          {"worker": False}, {"disco_pct": 10, "ram_pct": 10}, [])
    observador.registrar(db, observador.coletar_estado(acc_up, {}, agora=1000.0))
    observador.registrar(db, observador.coletar_estado(acc_down, {}, agora=5000.0))
    # janela curta em torno de 5000: só vê a amostra 'down' -> não flapa
    assert observador.flapping(db, "worker", janela_s=60.0, agora=5000.0) is False


# --- Progresso-por-verdade: costura injetada (NÃO fala com o Notion aqui) ---
def test_progresso_captura_usa_seam_double():
    contagens = {"curso-A": (65, 537), "curso-B": (10, 10)}
    prog = observador.progresso_captura(lambda c: contagens[c],
                                        ["curso-A", "curso-B"], agora=1000.0)
    by = {p.curso: p for p in prog}
    assert by["curso-A"].done == 65 and by["curso-A"].total == 537
    assert by["curso-A"].ts == 1000.0
    assert by["curso-B"].completo is True and by["curso-A"].completo is False


def test_coletar_estado_pluga_progresso_via_seam():
    prog_fn = lambda: observador.progresso_captura(lambda c: (1, 4),
                                                   ["curso-A"], agora=1000.0)
    est = observador.coletar_estado(_acesso(), {}, agora=1000.0, progresso_fn=prog_fn)
    assert est.progresso[0].curso == "curso-A"
    assert est.progresso[0].done == 1 and est.progresso[0].total == 4
    assert est.progresso[0].ts == 1000.0
