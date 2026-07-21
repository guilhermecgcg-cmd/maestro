"""VIGIA — autópsia dos filhos que a Athena spawna.

Dublês COM DENTES: a liveness do PID é um `pid_vivo` INJETÁVEL (um conjunto de PIDs
"vivos") — errar a detecção de morte FALHA o teste, sem depender de subprocesso real
aqui (o subprocesso REAL vive no harness de evidência). As fontes de stderr são arquivos
REAIS em disco (tail de verdade), e as autópsias são lidas de volta do JSON gravado —
se o exit_code ou o stderr_tail forem gravados errados, o teste pega.
"""
import json
import os
import tempfile

import pytest

from maestro import vigia


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _lock(lock_dir, conta, curso, pid):
    """Grava um lock DURÁVEL no MESMO formato do captura.LocalExecutor
    (sha256(conta)[:16].lock com {pid, course_url, conta})."""
    import hashlib
    slug = hashlib.sha256(str(conta).encode("utf-8")).hexdigest()[:16]
    path = os.path.join(lock_dir, slug + ".lock")
    with open(path, "w") as f:
        json.dump({"pid": pid, "course_url": curso, "conta": str(conta)}, f)
    return path


def _stderr(tmp, texto):
    path = os.path.join(tmp, "err-%d.log" % (abs(hash(texto)) % 100000))
    with open(path, "w") as f:
        f.write(texto)
    return path


def _vivos(*pids):
    s = set(pids)
    return lambda pid: pid in s


@pytest.fixture
def dirs():
    with tempfile.TemporaryDirectory() as d:
        lock_dir = os.path.join(d, "locks")
        aut_dir = os.path.join(d, "autopsias")
        os.makedirs(lock_dir)
        yield d, lock_dir, aut_dir


# --------------------------------------------------------------------------
# morte x vida
# --------------------------------------------------------------------------
def test_filho_vivo_nao_gera_obito(dirs):
    d, lock_dir, aut_dir = dirs
    _lock(lock_dir, "acme", "http://x", 4242)
    fonte = vigia.FonteFilho(exit_code=None, pid=4242)   # sem código, PID vivo
    obitos = vigia.autopsia(lock_dir, {"acme": fonte},
                            pid_vivo=_vivos(4242), autopsia_dir=aut_dir)
    assert obitos == []
    assert os.listdir(aut_dir) == [] if os.path.isdir(aut_dir) else True


def test_morte_por_exit_code(dirs):
    d, lock_dir, aut_dir = dirs
    _lock(lock_dir, "acme", "http://curso", 4242)
    err = _stderr(d, "boom\nsession expired\n")
    fonte = vigia.FonteFilho(exit_code=1, stderr_path=err, pid=4242)
    obitos = vigia.autopsia(lock_dir, {"acme": fonte},
                            pid_vivo=_vivos(), autopsia_dir=aut_dir)
    assert len(obitos) == 1
    o = obitos[0]
    assert o.conta == "acme"
    assert o.curso == "http://curso"           # veio do LOCK
    assert o.exit_code == 1
    assert "session expired" in o.stderr_tail


def test_morte_por_pid_morto_sem_exit_code(dirs):
    d, lock_dir, aut_dir = dirs
    _lock(lock_dir, "acme", "http://curso", 4242)
    # sem fonte: só o lock. PID 4242 NÃO está vivo => morte detectada pelo PID.
    obitos = vigia.autopsia(lock_dir, {},
                            pid_vivo=_vivos(), autopsia_dir=aut_dir)
    assert len(obitos) == 1
    assert obitos[0].exit_code is None          # código desconhecido (só o PID sumiu)
    assert obitos[0].curso == "http://curso"


def test_lock_de_intencao_pid_none_nao_e_morte(dirs):
    d, lock_dir, aut_dir = dirs
    _lock(lock_dir, "acme", "http://curso", None)   # lock de INTENÇÃO (spawn em curso)
    obitos = vigia.autopsia(lock_dir, {},
                            pid_vivo=_vivos(), autopsia_dir=aut_dir)
    assert obitos == []                             # não afirma morte sem sinal


def test_lock_ausente_usa_fonte(dirs):
    d, lock_dir, aut_dir = dirs
    err = _stderr(d, "kaboom\n")
    fonte = vigia.FonteFilho(exit_code=2, stderr_path=err, curso="http://sofonte")
    obitos = vigia.autopsia(lock_dir, {"conta9": fonte},
                            pid_vivo=_vivos(), autopsia_dir=aut_dir)
    assert len(obitos) == 1
    assert obitos[0].curso == "http://sofonte"      # sem lock, curso veio da FONTE
    assert obitos[0].exit_code == 2


# --------------------------------------------------------------------------
# autópsia em disco
# --------------------------------------------------------------------------
def test_autopsia_gravada_em_disco(dirs):
    d, lock_dir, aut_dir = dirs
    _lock(lock_dir, "acme", "http://curso", 4242)
    err = _stderr(d, "linha1\nsession expired\n")
    fonte = vigia.FonteFilho(exit_code=1, stderr_path=err, pid=4242)
    vigia.autopsia(lock_dir, {"acme": fonte},
                   pid_vivo=_vivos(), autopsia_dir=aut_dir)
    arqs = [x for x in os.listdir(aut_dir) if x.endswith(".json")]
    assert len(arqs) == 1
    with open(os.path.join(aut_dir, arqs[0])) as f:
        rec = json.load(f)
    assert rec["exit_code"] == 1
    assert "session expired" in rec["stderr_tail"]
    assert rec["conta"] == "acme"
    assert rec["curso"] == "http://curso"


def test_stderr_tail_limita_linhas(dirs):
    d, lock_dir, aut_dir = dirs
    _lock(lock_dir, "acme", "http://curso", 1)
    texto = "\n".join("linha%d" % i for i in range(200)) + "\n"
    err = _stderr(d, texto)
    fonte = vigia.FonteFilho(exit_code=1, stderr_path=err)
    obitos = vigia.autopsia(lock_dir, {"acme": fonte}, pid_vivo=_vivos(),
                            autopsia_dir=aut_dir, stderr_linhas=10)
    linhas = obitos[0].stderr_tail.strip().splitlines()
    assert len(linhas) == 10
    assert linhas[-1] == "linha199"                  # é o TAIL (últimas), não a cabeça


# --------------------------------------------------------------------------
# flapping
# --------------------------------------------------------------------------
def test_flapping_conta_flaps_na_janela(dirs):
    d, lock_dir, aut_dir = dirs
    err = _stderr(d, "morre\n")
    t = 1_000_000.0
    flaps = []
    for i in range(3):
        _lock(lock_dir, "acme", "http://curso", 1)   # re-nasce e re-morre
        fonte = vigia.FonteFilho(exit_code=1, stderr_path=err)
        obs = vigia.autopsia(lock_dir, {"acme": fonte}, pid_vivo=_vivos(),
                             autopsia_dir=aut_dir, agora=t + i * 60, janela_s=1800)
        flaps.append(obs[0].flaps_na_janela)
    assert flaps == [1, 2, 3]                        # 3 mortes em 3 min => flap=3


def test_flap_fora_da_janela_nao_conta(dirs):
    d, lock_dir, aut_dir = dirs
    err = _stderr(d, "morre\n")
    _lock(lock_dir, "acme", "http://curso", 1)
    vigia.autopsia(lock_dir, {"acme": vigia.FonteFilho(exit_code=1, stderr_path=err)},
                   pid_vivo=_vivos(), autopsia_dir=aut_dir, agora=1000.0, janela_s=1800)
    # 2ª morte MUITO depois (janela de 30min já passou): flap volta a 1.
    _lock(lock_dir, "acme", "http://curso", 1)
    obs = vigia.autopsia(lock_dir, {"acme": vigia.FonteFilho(exit_code=1, stderr_path=err)},
                         pid_vivo=_vivos(), autopsia_dir=aut_dir, agora=1000.0 + 999999,
                         janela_s=1800)
    assert obs[0].flaps_na_janela == 1


def test_flap_por_conta_e_isolado(dirs):
    d, lock_dir, aut_dir = dirs
    err = _stderr(d, "morre\n")
    t = 5000.0
    for conta in ("acme", "acme", "outra"):
        path = _lock(lock_dir, conta, "http://%s" % conta, 1)
        vigia.autopsia(lock_dir, {conta: vigia.FonteFilho(exit_code=1, stderr_path=err)},
                       pid_vivo=_vivos(), autopsia_dir=aut_dir, agora=t, janela_s=1800)
        os.remove(path)          # modela o _reap do executor: lock do morto some do disco
        t += 10
    # última morte foi 'outra' — deve ser flap 1 (isolada), não contaminada pelas 2 acme.
    recs = []
    for x in sorted(os.listdir(aut_dir)):
        with open(os.path.join(aut_dir, x)) as f:
            recs.append(json.load(f))
    outra = [r for r in recs if r["conta"] == "outra"]
    acme = [r for r in recs if r["conta"] == "acme"]
    assert outra[0]["flaps_na_janela"] == 1
    assert max(r["flaps_na_janela"] for r in acme) == 2
