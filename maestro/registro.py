"""Registro de projetos que o Maestro coordena. Projeto novo = uma entrada no YAML,
sem tocar no núcleo. O DATABASE_URL de um projeto (segredo) vem por env
(`database_url_env`), nunca no YAML."""
import os
from dataclasses import dataclass, field, replace

import yaml


@dataclass(frozen=True)
class Projeto:
    nome: str
    projeto_easypanel: str
    servicos: tuple
    saude: dict = field(default_factory=dict)
    adaptador: str = ""
    database_url: str = ""
    gerenciar: bool = False   # False = só monitora/avisa; True = pode agir (opt-in)


def carregar(path: str) -> list:
    with open(path) as f:
        dados = yaml.safe_load(f) or []
    out = []
    for d in dados:
        env_name = d.get("database_url_env")
        db = os.environ.get(env_name, "") if env_name else d.get("database_url", "")
        out.append(Projeto(nome=d["nome"], projeto_easypanel=d["projeto_easypanel"],
                           servicos=tuple(d.get("servicos", [])), saude=d.get("saude", {}),
                           adaptador=d.get("adaptador", ""), database_url=db,
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
