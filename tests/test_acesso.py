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


import pytest


def test_exec_sql_monta_comando_e_parseia_linhas():
    capturado = {}
    def fake_run(cmd):
        capturado["cmd"] = cmd
        return "1|capturando|123.0|\n2|falhou|0|erro\n__EXEC_OK__\n"
    a = Acesso(run_cmd=fake_run)
    linhas = a.exec_sql("cp_db", "SELECT 1", db="conhecimento", user="postgres")
    assert linhas == ["1|capturando|123.0|", "2|falhou|0|erro"]   # sentinela removida
    c = capturado["cmd"]
    assert "psql -U postgres -d conhecimento" in c
    assert "docker ps -qf name=cp_db" in c   # descobre o container pelo socket


def test_exec_sql_levanta_quando_container_nao_existe():
    a = Acesso(run_cmd=lambda cmd: "__EXEC_FAIL__no_container\n")
    with pytest.raises(RuntimeError):
        a.exec_sql("nao_existe", "SELECT 1", db="d")


def test_exec_sql_levanta_quando_psql_falha():
    a = Acesso(run_cmd=lambda cmd: "ERRO psql\n__EXEC_FAIL__psql\n")
    with pytest.raises(RuntimeError):
        a.exec_sql("cp_db", "SELECT bad", db="d")


def test_exec_sql_levanta_sem_sentinela():
    # falha silenciosa (stdout vazio) NÃO pode virar "0 linhas"
    a = Acesso(run_cmd=lambda cmd: "")
    with pytest.raises(RuntimeError):
        a.exec_sql("cp_db", "SELECT 1", db="d")


def test_exec_sql_rows_false_ok_com_sentinela():
    a = Acesso(run_cmd=lambda cmd: "UPDATE 1\n__EXEC_OK__\n")
    assert a.exec_sql("cp_db", "UPDATE x SET y=1", db="d", rows=False) == []
