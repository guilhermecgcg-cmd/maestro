"""LocalExecutor — a Athena DOMÉSTICA chamando o MOTOR DIRETO no Mac (subprocesso).

Dublês COM DENTES: FakeProc modela o CONTRATO real de Popen (`poll()` -> None enquanto
vivo, código quando encerra) e FakeSpawn registra CADA comando/env/cwd — errar o
módulo, o backend Whisper, a política headed/headless, o cwd, o anti-ban ou a
idempotência FALHA um teste. Nada de subprocesso real."""
import os

import pytest

from maestro.adaptadores import captura

C1 = "https://hotmart.com/pt-br/marketplace/produtos/x/products/111"
C2 = "https://hotmart.com/pt-br/marketplace/produtos/y/products/222"
MK = "https://minha.memberkit.com.br/321"
PY = "/opt/aula/.venv/bin/python"
DIR = "/opt/aula"


class FakeProc:
    """Modela um Popen: vivo (poll()->None) até `encerrar(code)`."""
    def __init__(self):
        self._code = None

    def poll(self):
        return self._code

    def encerrar(self, code=0):
        self._code = code


class FakeSpawn:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, *, env, cwd):
        proc = FakeProc()
        self.calls.append({"cmd": cmd, "env": env, "cwd": cwd, "proc": proc})
        return proc

    @property
    def procs(self):
        return [c["proc"] for c in self.calls]


def _exec(*cursos, spawn=None, **kw):
    return captura.LocalExecutor(
        cursos, motor_python=PY, motor_dir=DIR, spawn=spawn or FakeSpawn(), **kw)


def _hot(url, conta="a"):
    return captura.CursoLocal(url=url, conta=conta, plataforma="hotmart")


# ==========================================================================
# Comando: chama o MOTOR DIRETO (não enfileira), com Groq e cwd do motor
# ==========================================================================
def test_dispara_o_motor_direto_com_url_backend_groq_e_cwd():
    sp = FakeSpawn()
    ex = _exec(_hot(C1), spawn=sp)
    conf = ex.disparar(C1)
    assert conf                                            # confirmação truthy
    assert len(sp.calls) == 1
    call = sp.calls[0]
    # DENTES: comando é `<motor_python> -m motor.cli <url>` (chama o motor, não INSERT)
    assert call["cmd"] == [PY, "-m", "motor.cli", C1]
    assert call["env"]["WHISPER_BACKEND"] == "groq"        # INVIOLÁVEL Groq
    assert call["cwd"] == DIR                              # cwd p/ motor.config achar .env


def test_memberkit_usa_o_modulo_proprio():
    sp = FakeSpawn()
    _exec(captura.CursoLocal(url=MK, conta="mk", plataforma="memberkit"),
          spawn=sp).disparar(MK)
    assert sp.calls[0]["cmd"] == [PY, "-m", "motor.memberkit", MK]


# ==========================================================================
# INVIOLÁVEL: Hotmart HEADED; outras plataformas headless
# ==========================================================================
def test_hotmart_e_headed_nunca_headless():
    sp = FakeSpawn()
    _exec(_hot(C1), spawn=sp).disparar(C1)
    # DENTES: HEADLESS NÃO pode estar setado p/ hotmart (a sonda de sessão falha headless)
    assert sp.calls[0]["env"].get("HEADLESS", "") == ""


def test_hotmart_limpa_headless_herdado_do_ambiente(monkeypatch):
    # Mesmo com HEADLESS=1 vazando do ambiente, hotmart tem de sair HEADED.
    monkeypatch.setenv("HEADLESS", "1")
    sp = FakeSpawn()
    _exec(_hot(C1), spawn=sp).disparar(C1)
    assert "HEADLESS" not in sp.calls[0]["env"]


def test_outras_plataformas_sao_headless():
    sp = FakeSpawn()
    _exec(captura.CursoLocal(url=MK, conta="mk", plataforma="memberkit"),
          spawn=sp).disparar(MK)
    assert sp.calls[0]["env"]["HEADLESS"] == "1"


# ==========================================================================
# INVIOLÁVEL ANTI-BAN: 1 captura por conta; contas diferentes em paralelo
# ==========================================================================
def test_nunca_duas_capturas_na_mesma_conta():
    sp = FakeSpawn()
    ex = _exec(_hot(C1, "conta-A"), _hot(C2, "conta-A"), spawn=sp)
    ex.disparar(C1)                                        # C1 roda
    with pytest.raises(captura.ContaOcupada):              # DENTES: recusa a 2ª na conta
        ex.disparar(C2)
    assert len(sp.calls) == 1                              # só UM processo aberto


def test_contas_diferentes_capturam_em_paralelo():
    sp = FakeSpawn()
    ex = _exec(_hot(C1, "conta-A"), _hot(C2, "conta-B"), spawn=sp)
    ex.disparar(C1)
    ex.disparar(C2)                                        # conta diferente: NÃO bloqueia
    assert len(sp.calls) == 2
    assert ex.conta_ocupada("conta-A") and ex.conta_ocupada("conta-B")


def test_conta_libera_quando_o_processo_encerra():
    sp = FakeSpawn()
    ex = _exec(_hot(C1, "conta-A"), _hot(C2, "conta-A"), spawn=sp)
    ex.disparar(C1)
    sp.procs[0].encerrar(0)                                # C1 terminou
    ex.disparar(C2)                                        # agora a conta está livre
    assert len(sp.calls) == 2
    assert not ex.curso_ativo(C1) and ex.curso_ativo(C2)


# ==========================================================================
# IDEMPOTENTE POR CURSO: não re-spawna um curso que já roda (não martela)
# ==========================================================================
def test_idempotente_por_curso_nao_reabre_processo():
    sp = FakeSpawn()
    ex = _exec(_hot(C1), spawn=sp)
    ex.disparar(C1)
    conf = ex.disparar(C1)                                 # 2º disparo com C1 ainda vivo
    assert conf                                            # devolve confirmação
    assert len(sp.calls) == 1                              # DENTES: NÃO abriu 2º processo


def test_curso_ativo_reflete_o_ciclo_de_vida():
    sp = FakeSpawn()
    ex = _exec(_hot(C1), spawn=sp)
    assert not ex.curso_ativo(C1)
    ex.disparar(C1)
    assert ex.curso_ativo(C1)
    sp.procs[0].encerrar(0)
    assert not ex.curso_ativo(C1)


# ==========================================================================
# FAIL-CLOSED: plataforma desconhecida / curso sem metadados -> LEVANTA
# ==========================================================================
def test_plataforma_desconhecida_levanta_e_nao_spawna():
    sp = FakeSpawn()
    ex = _exec(captura.CursoLocal(url="https://kiwify.com.br/z", conta="k",
                                  plataforma="kiwify"), spawn=sp)
    with pytest.raises(RuntimeError):
        ex.disparar("https://kiwify.com.br/z")
    assert sp.calls == []                                  # nada disparado às cegas


def test_curso_sem_metadados_levanta():
    ex = _exec(_hot(C1))
    with pytest.raises(RuntimeError):
        ex.disparar("https://hotmart.com/desconhecido")


# ==========================================================================
# Groq key injetada quando fornecida (senão vem do chave-groq.txt via motor.config)
# ==========================================================================
def test_groq_key_injetada_quando_fornecida():
    sp = FakeSpawn()
    _exec(_hot(C1), spawn=sp, groq_key="gsk-abc").disparar(C1)
    assert sp.calls[0]["env"]["GROQ_API_KEY"] == "gsk-abc"
