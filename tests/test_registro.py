from maestro.registro import carregar


def test_carrega_e_resolve_db_por_env(tmp_path, monkeypatch):
    y = tmp_path / "p.yaml"
    y.write_text("- nome: c\n  projeto_easypanel: cp\n  servicos: [a, b]\n"
                 "  adaptador: conhecimento\n  database_url_env: FOO_DB\n")
    monkeypatch.setenv("FOO_DB", "postgresql://x")
    projs = carregar(str(y))
    assert len(projs) == 1
    p = projs[0]
    assert p.nome == "c" and p.servicos == ("a", "b")
    assert p.database_url == "postgresql://x"   # resolvido do env, não do YAML


def test_mesclar_descobre_novo_como_monitorado():
    from maestro.registro import mesclar, Projeto
    overlay = [Projeto("conhecimento", "conhecimentoinfinito", ("worker",),
                       adaptador="conhecimento", gerenciar=True)]
    descobertos = {"conhecimentoinfinito": ["worker", "db"], "loja": ["api"]}
    projs = {p.projeto_easypanel: p for p in mesclar(descobertos, overlay)}
    # projeto conhecido: mantém adaptador/gerenciar + une serviços
    assert projs["conhecimentoinfinito"].adaptador == "conhecimento"
    assert projs["conhecimentoinfinito"].gerenciar is True
    assert set(projs["conhecimentoinfinito"].servicos) == {"worker", "db"}
    # projeto NOVO (só descoberto): entra monitorado, sem agir
    assert projs["loja"].gerenciar is False and projs["loja"].servicos == ("api",)
