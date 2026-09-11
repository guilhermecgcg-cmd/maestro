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


# --------------------------------------------------------------------------
# SAÍDA LIMPA (exit 0) — não é MORTE: não infla o flap nem vira alarme
# --------------------------------------------------------------------------
def test_saida_limpa_exit0_marca_obito_e_flap_zero(dirs):
    # DENTES: exit_code 0 é SAÍDA LIMPA (curso concluído/nada pendente), NÃO morte.
    # O óbito é marcado (saida_limpa) e o flap dela é 0 — não conta como morte na janela.
    d, lock_dir, aut_dir = dirs
    _lock(lock_dir, "acme", "http://curso", 1)
    obs = vigia.autopsia(lock_dir, {"acme": vigia.FonteFilho(exit_code=0)},
                         pid_vivo=_vivos(), autopsia_dir=aut_dir, agora=1000.0)
    assert len(obs) == 1
    assert obs[0].saida_limpa is True
    assert obs[0].exit_code == 0
    assert obs[0].flaps_na_janela == 0          # saída limpa não é um evento de flap
    # e NÃO polui o diretório de autópsias (que é de MORTES)
    arqs = [x for x in os.listdir(aut_dir) if x.endswith(".json")] if os.path.isdir(aut_dir) else []
    assert arqs == []


def test_saidas_limpas_nao_inflam_flap_de_morte_real(dirs):
    # DENTES do bug: N saídas limpas (exit 0) na MESMA conta NÃO podem inflar o flap de
    # uma morte real subsequente. No código bugado, cada exit-0 gravava autópsia e a
    # morte real via flap=N+1 (falso alarme "FLAP: N mortes na janela").
    d, lock_dir, aut_dir = dirs
    for i in range(5):
        _lock(lock_dir, "acme", "http://curso", 1)
        vigia.autopsia(lock_dir, {"acme": vigia.FonteFilho(exit_code=0)},
                       pid_vivo=_vivos(), autopsia_dir=aut_dir, agora=1000.0 + i,
                       janela_s=1800)
    _lock(lock_dir, "acme", "http://curso", 1)
    err = _stderr(d, "boom real\n")
    obs = vigia.autopsia(lock_dir, {"acme": vigia.FonteFilho(exit_code=2, stderr_path=err)},
                         pid_vivo=_vivos(), autopsia_dir=aut_dir, agora=1006.0, janela_s=1800)
    assert obs[0].saida_limpa is False
    assert obs[0].flaps_na_janela == 1          # as 5 saídas limpas NÃO contaram


def test_saida_limpa_nao_conta_flap_mesmo_com_autopsia_legada_em_disco(dirs):
    # DENTES de retrocompat: autópsias exit_code=0 JÁ gravadas em disco (o bug antigo
    # deixou dezenas) NÃO podem contar como flap de uma morte real — o contador filtra.
    d, lock_dir, aut_dir = dirs
    os.makedirs(aut_dir, exist_ok=True)
    for i in range(9):                          # legado do bug: exit_code=0 no disco
        import json as _json
        with open(os.path.join(aut_dir, "leg-%02d.json" % i), "w") as f:
            _json.dump({"conta": "acme", "exit_code": 0, "ts_epoch": 1000.0 + i}, f)
    _lock(lock_dir, "acme", "http://curso", 1)
    err = _stderr(d, "morte de verdade\n")
    obs = vigia.autopsia(lock_dir, {"acme": vigia.FonteFilho(exit_code=1, stderr_path=err)},
                         pid_vivo=_vivos(), autopsia_dir=aut_dir, agora=1005.0, janela_s=1800)
    assert obs[0].flaps_na_janela == 1          # legado exit-0 ignorado


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


# --------------------------------------------------------------------------
# MORTE SÓ-DE-LOCK (item 1 da rodada 3): captura de uma encarnação ANTERIOR do loop. O
# `drenar_obitos` só conhece os filhos da encarnação atual, então a autópsia dessas
# mortes saía com stderr VAZIO. Agora: o .err da conta (`stderr_path_de`, o
# LocalExecutor._stderr_path) + o mtime do lock contra o boot do Mac (`boot_ts`).
# --------------------------------------------------------------------------
def _err_da_conta(d):
    return lambda conta: os.path.join(d, "motor-logs", "%s.err" % conta)


def _escrever_err(d, conta, texto, mtime=None):
    path = _err_da_conta(d)(conta)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(texto)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_morte_so_de_lock_le_o_err_da_conta(dirs):
    # DENTES (item 1a): sem o .err a cauda desta morte era "" e a causa, cega.
    d, lock_dir, aut_dir = dirs
    lk = _lock(lock_dir, "acme", "http://curso", 4242)
    os.utime(lk, (1000.0, 1000.0))
    _escrever_err(d, "acme", "linha 1\nTimeoutError: Page.goto: Timeout 30000ms exceeded.\n",
                  mtime=1500.0)
    obs = vigia.autopsia(lock_dir, {}, pid_vivo=_vivos(), autopsia_dir=aut_dir,
                         agora=2000.0, stderr_path_de=_err_da_conta(d), boot_ts=10.0)
    assert len(obs) == 1 and obs[0].exit_code is None
    assert "TimeoutError: Page.goto" in obs[0].stderr_tail
    assert obs[0].lock_mtime == 1000.0
    with open(obs[0].autopsia_path) as f:
        rec = json.load(f)
    assert "TimeoutError" in rec["stderr_tail"] and rec["detectado_por"] == "pid"


def test_err_mais_velho_que_o_lock_nao_e_desta_morte(dirs):
    # O .err é TRUNCADO a cada disparo e o lock é regravado no mesmo disparo: um .err
    # mais VELHO que o lock é de um run anterior (o tee falhou) — atribuir a cauda dele a
    # esta morte seria mentir a causa.
    d, lock_dir, aut_dir = dirs
    lk = _lock(lock_dir, "acme", "http://curso", 4242)
    os.utime(lk, (1000.0, 1000.0))
    _escrever_err(d, "acme", "SESSÃO MORTA de um run antigo\n", mtime=1000.0 - 60)
    obs = vigia.autopsia(lock_dir, {}, pid_vivo=_vivos(), autopsia_dir=aut_dir,
                         agora=2000.0, stderr_path_de=_err_da_conta(d), boot_ts=10.0)
    assert obs[0].stderr_tail == ""


def test_err_da_conta_nao_sobrepoe_a_fonte_do_filho_desta_encarnacao(dirs):
    # Filho DESTA encarnação (fonte com exit_code e stderr próprio): a regra só-de-lock
    # não se aplica — nem o .err "da conta" nem o boot mexem no óbito.
    d, lock_dir, aut_dir = dirs
    lk = _lock(lock_dir, "acme", "http://curso", 4242)
    os.utime(lk, (1000.0, 1000.0))
    _escrever_err(d, "acme", "OUTRA cauda\n", mtime=1500.0)
    fonte = vigia.FonteFilho(exit_code=1, stderr_tail="cauda do filho\n", pid=4242)
    obs = vigia.autopsia(lock_dir, {"acme": fonte}, pid_vivo=_vivos(), autopsia_dir=aut_dir,
                         agora=2000.0, stderr_path_de=_err_da_conta(d), boot_ts=5000.0)
    assert obs[0].stderr_tail == "cauda do filho"
    assert obs[0].lock_antes_do_boot is False and obs[0].lock_mtime is None


@pytest.mark.parametrize("lock_mtime,boot,antes", [
    (1000.0, 2000.0, True),                       # disparada antes do boot: reinício
    (3000.0, 2000.0, False),                      # disparada depois do boot: órfã de bounce
])
def test_lock_contra_o_boot_do_mac(dirs, lock_mtime, boot, antes):
    # DENTES (item 1b): o óbito carrega o veredito "o lock é anterior ao boot" (e os
    # números, p/ a autópsia em disco explicar a decisão).
    d, lock_dir, aut_dir = dirs
    lk = _lock(lock_dir, "acme", "http://curso", 4242)
    os.utime(lk, (lock_mtime, lock_mtime))
    obs = vigia.autopsia(lock_dir, {}, pid_vivo=_vivos(), autopsia_dir=aut_dir,
                         agora=9000.0, boot_ts=boot)
    assert obs[0].lock_antes_do_boot is antes
    assert obs[0].boot_ts == boot and obs[0].lock_mtime == lock_mtime
    with open(obs[0].autopsia_path) as f:
        rec = json.load(f)
    assert rec["lock_antes_do_boot"] is antes and rec["boot_ts"] == boot


def test_boot_so_e_lido_quando_ha_morte_so_de_lock(dirs, monkeypatch):
    # O sysctl é um subprocesso: só roda se houver morte só-de-lock (e uma vez por
    # varredura). Sem boot_ts injetado, vale o do sistema.
    d, lock_dir, aut_dir = dirs
    chamadas = []
    monkeypatch.setattr(vigia, "boot_ts_do_mac", lambda: chamadas.append(1) or 1500.0)
    fonte = vigia.FonteFilho(exit_code=1, stderr_tail="x")
    vigia.autopsia(lock_dir, {"acme": fonte}, pid_vivo=_vivos(), autopsia_dir=aut_dir)
    assert chamadas == []
    for conta, mt in (("a", 1000.0), ("b", 2000.0)):
        os.utime(_lock(lock_dir, conta, "http://%s" % conta, 4242), (mt, mt))
    obs = vigia.autopsia(lock_dir, {}, pid_vivo=_vivos(), autopsia_dir=aut_dir)
    assert chamadas == [1]
    assert {o.conta: o.lock_antes_do_boot for o in obs} == {"a": True, "b": False}


def test_boot_ts_do_mac_le_o_kern_boottime_real():
    # Produção: o boot vem do `sysctl -n kern.boottime` (só leitura). Conferido contra
    # uma leitura independente do mesmo sysctl.
    import subprocess
    import sys
    import time as _time
    if sys.platform != "darwin":
        pytest.skip("kern.boottime é do macOS")
    saida = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.boottime"],
                           capture_output=True, text=True, timeout=5).stdout
    sec = int(saida.split("sec =")[1].split(",")[0])
    boot = vigia.boot_ts_do_mac()
    assert boot is not None and int(boot) == sec
    assert boot < _time.time()


def test_flap_ignora_morte_anotada_sem_falha(dirs):
    # Morte SEM FALHA da captura (reinício do Mac / órfã limpa — anotada pela causa no
    # JSON) não é evento de flap: não infla o contador das próximas mortes da conta.
    d, lock_dir, aut_dir = dirs
    os.makedirs(aut_dir, exist_ok=True)
    for i in range(4):
        with open(os.path.join(aut_dir, "reboot-%d.json" % i), "w") as f:
            json.dump({"conta": "acme", "exit_code": None, "ts_epoch": 1000.0 + i,
                       "sem_falha": True}, f)
    _lock(lock_dir, "acme", "http://curso", 1)
    obs = vigia.autopsia(lock_dir, {"acme": vigia.FonteFilho(exit_code=1, stderr_tail="x")},
                         pid_vivo=_vivos(), autopsia_dir=aut_dir, agora=1005.0, janela_s=1800)
    assert obs[0].flaps_na_janela == 1
