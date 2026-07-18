"""Registro de projetos que o Maestro coordena. Projeto novo = uma entrada no YAML,
sem tocar no núcleo. O DATABASE_URL de um projeto (segredo) vem por env
(`database_url_env`), nunca no YAML."""
import os
from dataclasses import dataclass, field

import yaml


@dataclass(frozen=True)
class Projeto:
    nome: str
    projeto_easypanel: str
    servicos: tuple
    saude: dict = field(default_factory=dict)
    adaptador: str = ""
    database_url: str = ""


def carregar(path: str) -> list:
    with open(path) as f:
        dados = yaml.safe_load(f) or []
    out = []
    for d in dados:
        env_name = d.get("database_url_env")
        db = os.environ.get(env_name, "") if env_name else d.get("database_url", "")
        out.append(Projeto(nome=d["nome"], projeto_easypanel=d["projeto_easypanel"],
                           servicos=tuple(d.get("servicos", [])), saude=d.get("saude", {}),
                           adaptador=d.get("adaptador", ""), database_url=db))
    return out
