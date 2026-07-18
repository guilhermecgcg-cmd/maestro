from maestro.sentinela import checar
from maestro.acesso import Servico


def _snap(**kw):
    base = {"servicos": {}, "fila": [], "captura": {},
            "recursos": {"disco_pct": 10, "ram_pct": 10}, "agora": 1000.0}
    base.update(kw); return base


def test_detecta_servico_caido():
    ps = checar(_snap(servicos={"worker": Servico("worker", up=False, restarting=False)}))
    assert any(p.tipo == "servico_caido" and p.alvo == "worker" for p in ps)


def test_detecta_restart_loop():
    ps = checar(_snap(servicos={"painel-api": Servico("painel-api", up=False, restarting=True)}))
    assert any(p.tipo == "servico_restart_loop" for p in ps)



def test_disco_alto():
    ps = checar(_snap(recursos={"disco_pct": 95, "ram_pct": 10}))
    assert any(p.tipo == "disco_alto" for p in ps)


def test_saudavel_nao_gera_problema():
    ps = checar(_snap(servicos={"worker": Servico("worker", up=True, restarting=False)}))
    assert ps == []


def test_servico_up_mas_doente():
    snap = _snap(servicos={"painel-api": Servico("painel-api", up=True, restarting=False)},
                 saude={"painel-api": False})
    ps = checar(snap)
    assert any(p.tipo == "servico_doente" and p.alvo == "painel-api" for p in ps)


def test_servico_up_e_saudavel_ok():
    snap = _snap(servicos={"painel-api": Servico("painel-api", up=True, restarting=False)},
                 saude={"painel-api": True})
    assert checar(snap) == []
