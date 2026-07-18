from maestro.adaptadores import conhecimento
from maestro.sentinela import Problema
from maestro.registro import Projeto


def _proj():
    # porta 1 = connection refused rápido (connect_timeout cobre)
    return Projeto("c", "cp", ("worker",), {}, "conhecimento", "postgresql://u@127.0.0.1:1/x")


def test_job_falhou_escala():
    ac = conhecimento.resolver(Problema("job_falhou", "3", "erro", "aviso"), None, _proj())
    assert ac.escalar and not ac.executada


def test_checar_db_inacessivel_retorna_vazio():
    assert conhecimento.checar(_proj()) == []   # sem estourar (o núcleo já cobre down)
