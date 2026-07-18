from maestro.acesso import Acesso, Servico


def test_servicos_parseia_docker_ps():
    saida = ("Up 2 hours  conhecimentoinfinito_worker.1.x\n"
             "Restarting (1) 3s  outroprojeto_api.1.y\n")
    a = Acesso(run_cmd=lambda cmd: saida)
    s = a.servicos()
    assert s["worker"] == Servico("worker", True, False)
    assert s["api"] == Servico("api", False, True)


def test_saude_http():
    a = Acesso(probe=lambda url: url.endswith("/ok"))
    assert a.saude_http({"a": "http://x/ok", "b": "http://y/bad"}) == {"a": True, "b": False}


def test_redeploy_usa_projeto_do_registro():
    chamado = {}
    a = Acesso(http_post=lambda path, body: chamado.update({"path": path, "body": body}))
    a.redeploy("painel-api", "conhecimentoinfinito")
    assert chamado["body"]["json"]["projectName"] == "conhecimentoinfinito"
    assert chamado["body"]["json"]["serviceName"] == "painel-api"


def test_descobrir_projetos():
    data = {"json": {"services": [
        {"projectName": "conhecimentoinfinito", "name": "worker"},
        {"projectName": "conhecimentoinfinito", "name": "db"},
        {"projectName": "loja", "name": "api"}]}}
    a = Acesso(http_get=lambda path: data)
    d = a.descobrir_projetos()
    assert d["conhecimentoinfinito"] == ["worker", "db"]
    assert d["loja"] == ["api"]
