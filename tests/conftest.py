"""Fixtures globais da suíte do maestro."""
import os

import pytest

from maestro.adaptadores import captura


@pytest.fixture(autouse=True)
def _tabela_de_processos_sem_motor(monkeypatch):
    """A tabela de processos REAL desta máquina (que roda o daemon e motores de verdade)
    nunca entra num teste por acidente: por padrão, o guard de motor fora do daemon
    (`captura._listar_processos`) vê só o próprio pytest. Quem testa o guard injeta
    `processos_fn`; o teste de fumaça do `ps` real desfaz este patch (`monkeypatch.undo()`)."""
    monkeypatch.setattr(captura, "_listar_processos",
                        lambda: [(os.getpid(), 1, "python -m pytest")])
