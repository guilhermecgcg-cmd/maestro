"""Registro de projetos que o Maestro coordena. Projeto novo = uma entrada no YAML,
sem tocar no núcleo. Segredos não vão no YAML: o acesso ao banco de um projeto é
feito via `docker exec` no container do banco (db_container/db_name/db_user), sem
senha e sem DATABASE_URL — o Maestro entra de dentro do container pelo socket."""
from dataclasses import dataclass, field, replace

import yaml


@dataclass(frozen=True)
class Projeto:
    nome: str
    projeto_easypanel: str
    servicos: tuple
    saude: dict = field(default_factory=dict)
    adaptador: str = ""
    db_container: str = ""       # container Postgres do projeto (match p/ docker ps)
    db_name: str = ""            # nome do banco dentro do container
    db_user: str = "postgres"    # usuário local (trust no socket do container)
    app_container: str = ""      # container do APP p/ `docker exec` de rotinas (ex.: reconcile)
    gerenciar: bool = False      # False = só monitora/avisa; True = pode agir (opt-in)


def carregar(path: str) -> list:
    with open(path) as f:
        dados = yaml.safe_load(f) or []
    out = []
    for d in dados:
        out.append(Projeto(nome=d["nome"], projeto_easypanel=d["projeto_easypanel"],
                           servicos=tuple(d.get("servicos", [])), saude=d.get("saude", {}),
                           adaptador=d.get("adaptador", ""),
                           db_container=d.get("db_container", ""),
                           db_name=d.get("db_name", ""),
                           db_user=d.get("db_user", "postgres"),
                           app_container=d.get("app_container", ""),
                           gerenciar=d.get("gerenciar", True)))  # configurado = gerenciado
    return out


def mesclar(descobertos: dict, overlay: list) -> list:
    """Funde a auto-descoberta (Easypanel) com o overlay configurado. Projeto
    descoberto E configurado usa o overlay (health/adaptador/gerenciar) + une os
    serviços; descoberto-só entra como MONITORADO (gerenciar=False)."""
    por_ep = {p.projeto_easypanel: p for p in overlay}
    out = []
    for proj_ep, svcs in descobertos.items():
        if proj_ep in por_ep:
            base = por_ep[proj_ep]
            out.append(replace(base, servicos=tuple(sorted(set(base.servicos) | set(svcs)))))
        else:
            out.append(Projeto(nome=proj_ep, projeto_easypanel=proj_ep,
                              servicos=tuple(svcs), gerenciar=False))
    return out
