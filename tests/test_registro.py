from maestro.registro import carregar


def test_carrega_le_db_container_e_gerenciar(tmp_path):
    y = tmp_path / "p.yaml"
    y.write_text("- nome: c\n  projeto_easypanel: cp\n  servicos: [a, b]\n"
                 "  adaptador: conhecimento\n  db_container: cp_db\n"
                 "  db_name: conhecimento\n  db_user: postgres\n")
    projs = carregar(str(y))
    assert len(projs) == 1
    p = projs[0]
    assert p.nome == "c" and p.servicos == ("a", "b")
    assert p.db_container == "cp_db" and p.db_name == "conhecimento" and p.db_user == "postgres"
    assert p.gerenciar is True   # configurado no YAML = gerenciado por padrão


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
