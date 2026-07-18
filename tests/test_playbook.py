from maestro.playbook import resolver
from maestro.sentinela import Problema


class _Acesso:
    def __init__(self): self.chamadas = []
    def restart(self, nome): self.chamadas.append(("restart", nome))
    def reenqueue(self, job_id): self.chamadas.append(("reenqueue", job_id))


def test_servico_caido_reinicia():
    a = _Acesso()
    ac = resolver(Problema("servico_caido", "painel-api", "", "critico"), a)
    assert ac.executada and not ac.escalar
    assert ("restart", "painel-api") in a.chamadas



def test_disco_alto_escala_nao_age():
    a = _Acesso()
    ac = resolver(Problema("disco_alto", "vps", "95%", "critico"), a)
    assert ac.escalar and not ac.executada

