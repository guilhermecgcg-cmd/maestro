"""LocalExecutor — a Athena DOMÉSTICA chamando o MOTOR DIRETO no Mac (subprocesso).

Dublês COM DENTES: FakeProc modela o CONTRATO real de Popen (`poll()` -> None enquanto
vivo, código quando encerra; `pid` estável) e FakeSpawn registra CADA comando/env/cwd —
errar o módulo, o backend Whisper, a política headed/headless, o cwd, o anti-ban ou a
idempotência FALHA um teste. Nada de subprocesso real.

MundoProc modela a TABELA DE PROCESSOS DO SO — quais PIDs estão vivos AGORA — e é o que
`pid_vivo` (o `os.kill(pid, 0)` do executor) consulta. É COMPARTILHÁVEL entre encarnações
do LocalExecutor: é isso que dá DENTES ao teste de RESTART (um executor novo, com o
processo antigo AINDA vivo na tabela, lê o lock DURÁVEL em disco e NÃO re-dispara)."""
import glob
import json
import os
import tempfile

import pytest

from maestro.adaptadores import captura

C1 = "https://hotmart.com/pt-br/marketplace/produtos/x/products/111"
C2 = "https://hotmart.com/pt-br/marketplace/produtos/y/products/222"
MK = "https://minha.memberkit.com.br/321"
PY = "/opt/aula/.venv/bin/python"
DIR = "/opt/aula"


class FakeProc:
    """Modela um Popen: vivo (poll()->None) até `encerrar(code)`. `pid` único e estável —
    é o que vai para o lockfile durável e o que `pid_vivo` sonda."""
    _seq = 4000

    def __init__(self):
        self._code = None
        FakeProc._seq += 1
        self.pid = FakeProc._seq

    def poll(self):
        return self._code

    def encerrar(self, code=0):
        self._code = code


class MundoProc:
    """Tabela de processos compartilhada entre encarnações do executor. `vivo(pid)` é o
    dublê do `os.kill(pid, 0)`: True enquanto o processo daquele PID não encerrou."""
    def __init__(self):
        self.procs = {}

    def novo(self):
        proc = FakeProc()
        self.procs[proc.pid] = proc
        return proc

    def vivo(self, pid):
        p = self.procs.get(pid)
        return p is not None and p.poll() is None


class FakeSpawn:
    def __init__(self, mundo=None):
        self.calls = []
        self.mundo = mundo or MundoProc()

    def __call__(self, cmd, *, env, cwd):
        proc = self.mundo.novo()
        self.calls.append({"cmd": cmd, "env": env, "cwd": cwd, "proc": proc})
        return proc

    @property
    def procs(self):
        return [c["proc"] for c in self.calls]


def _exec(*cursos, spawn=None, lock_dir=None, pid_vivo=None, **kw):
    spawn = spawn or FakeSpawn()
    return captura.LocalExecutor(
        cursos, motor_python=PY, motor_dir=DIR, spawn=spawn,
        lock_dir=lock_dir or tempfile.mkdtemp(),
        pid_vivo=pid_vivo or spawn.mundo.vivo, **kw)


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


# ==========================================================================
# INVIOLÁVEL (achado BAIXO): extra_env NUNCA clobbera os invioláveis (Groq/HEADLESS).
# Os invioláveis são aplicados DEPOIS do extra_env -> vencem.
# ==========================================================================
def test_extra_env_nao_clobbera_os_invioaveis():
    sp = FakeSpawn()
    # extra_env HOSTIL: tenta forçar backend local e HEADLESS num curso hotmart.
    _exec(_hot(C1), spawn=sp,
          extra_env={"WHISPER_BACKEND": "local", "HEADLESS": "1", "X_QUALQUER": "ok"}
          ).disparar(C1)
    env = sp.calls[0]["env"]
    # DENTES: se os invioláveis fossem aplicados ANTES do extra_env (o bug), estes 2
    # asserts quebrariam — o extra_env teria vencido.
    assert env["WHISPER_BACKEND"] == "groq"                # inviolável Groq venceu
    assert "HEADLESS" not in env                           # hotmart HEADED venceu
    assert env["X_QUALQUER"] == "ok"                       # extra_env inócuo preservado


def test_extra_env_nao_liga_headless_em_hotmart_via_clobber():
    sp = FakeSpawn()
    _exec(captura.CursoLocal(url=MK, conta="mk", plataforma="memberkit"), spawn=sp,
          extra_env={"WHISPER_BACKEND": "openai"}).disparar(MK)
    env = sp.calls[0]["env"]
    assert env["WHISPER_BACKEND"] == "groq"                # inviolável vence mesmo em memberkit
    assert env["HEADLESS"] == "1"                          # política headless preservada


# ==========================================================================
# INVIOLÁVEL ANTI-BAN DURÁVEL (achado ALTO): o guard 1-por-conta + idempotência-por-curso
# SOBREVIVE a um restart/crash do loop, porque vive num LOCKFILE em disco (PID + curso),
# não só no dict in-memory. O processo de captura sobrevive (start_new_session); um
# executor NOVO, com o processo antigo AINDA vivo, NÃO pode re-disparar.
# ==========================================================================
def test_restart_nao_redispara_o_mesmo_curso_com_processo_antigo_vivo(tmp_path):
    mundo = MundoProc()                                    # tabela de PIDs compartilhada
    lock = str(tmp_path)                                   # lock DURÁVEL compartilhado

    # Encarnação 1: dispara C1 na conta-A (processo desacoplado, "horas").
    sp1 = FakeSpawn(mundo)
    ex1 = _exec(_hot(C1, "conta-A"), spawn=sp1, lock_dir=lock, pid_vivo=mundo.vivo)
    assert ex1.disparar(C1) == f"local_iniciada:{C1}"
    assert len(sp1.calls) == 1                             # 1 processo real

    # >>> RESTART <<< o loop crasha/reinicia: NOVO executor, _procs={} do zero. Mas o
    # processo antigo (pid em sp1) CONTINUA vivo na tabela do SO (mundo).
    sp2 = FakeSpawn(mundo)
    ex2 = _exec(_hot(C1, "conta-A"), spawn=sp2, lock_dir=lock, pid_vivo=mundo.vivo)
    conf = ex2.disparar(C1)                                # relê Notion, acha PARCIAL, tenta C1
    # DENTES: NÃO abriu 2º processo do MESMO curso (idempotência durável). Remover o lock
    # durável faria este disparo abrir um 2º browser na mesma conta = ban + captura dupla.
    assert conf == f"ja_capturando:{C1}"
    assert len(sp2.calls) == 0                             # nenhum spawn novo pós-restart


def test_restart_recusa_2o_curso_na_mesma_conta_do_processo_antigo(tmp_path):
    mundo = MundoProc()
    lock = str(tmp_path)
    sp1 = FakeSpawn(mundo)
    ex1 = _exec(_hot(C1, "conta-A"), _hot(C2, "conta-A"), spawn=sp1,
                lock_dir=lock, pid_vivo=mundo.vivo)
    ex1.disparar(C1)                                       # C1 roda na conta-A

    # restart: novo executor; C1 antigo ainda vivo. Tentar C2 (mesma conta) = 2ª sessão.
    sp2 = FakeSpawn(mundo)
    ex2 = _exec(_hot(C1, "conta-A"), _hot(C2, "conta-A"), spawn=sp2,
                lock_dir=lock, pid_vivo=mundo.vivo)
    with pytest.raises(captura.ContaOcupada):              # DENTES: anti-ban durável
        ex2.disparar(C2)
    assert len(sp2.calls) == 0                             # nada disparado na conta ocupada


def test_restart_libera_a_conta_quando_o_processo_antigo_morreu(tmp_path):
    # O lado inverso: se o processo antigo MORREU (crash da captura, não só do loop), o
    # lock é obsoleto — a conta LIBERA e o novo executor PODE re-disparar (retomar).
    mundo = MundoProc()
    lock = str(tmp_path)
    sp1 = FakeSpawn(mundo)
    ex1 = _exec(_hot(C1, "conta-A"), spawn=sp1, lock_dir=lock, pid_vivo=mundo.vivo)
    ex1.disparar(C1)
    sp1.procs[0].encerrar(1)                               # processo de captura MORREU

    sp2 = FakeSpawn(mundo)
    ex2 = _exec(_hot(C1, "conta-A"), spawn=sp2, lock_dir=lock, pid_vivo=mundo.vivo)
    assert ex2.disparar(C1) == f"local_iniciada:{C1}"      # PID morto -> conta livre -> retoma
    assert len(sp2.calls) == 1


def test_restart_curso_ativo_e_conta_ocupada_leem_o_lock_duravel(tmp_path):
    mundo = MundoProc()
    lock = str(tmp_path)
    ex1 = _exec(_hot(C1, "conta-A"), spawn=FakeSpawn(mundo), lock_dir=lock,
                pid_vivo=mundo.vivo)
    ex1.disparar(C1)

    ex2 = _exec(_hot(C1, "conta-A"), spawn=FakeSpawn(mundo), lock_dir=lock,
                pid_vivo=mundo.vivo)
    # o executor NOVO enxerga o estado durável, não seu _procs vazio.
    assert ex2.curso_ativo(C1) is True
    assert ex2.conta_ocupada("conta-A") is True


# ==========================================================================
# TOCTOU (achado MÉDIO): o lock tem de ser gravado ANTES do spawn (lock de
# INTENÇÃO, pid=None), não DEPOIS. Um crash na janela sub-ms entre spawn e a
# escrita do lock deixaria uma captura ÓRFÃ (start_new_session) sem lock em
# disco -> restart releria o Notion parcial, veria a conta livre e re-dispararia
# = 2 browsers na mesma conta = ban + captura dupla.
# ==========================================================================
def test_disparar_grava_lock_de_intencao_ANTES_do_spawn(tmp_path):
    lock = str(tmp_path)
    visto = {}

    def spawn_que_espia(cmd, *, env, cwd):
        # No MOMENTO do spawn, o lock de INTENÇÃO já tem de existir em disco: é isso
        # que fecha o TOCTOU. Se o lock só fosse gravado DEPOIS (o bug), aqui não
        # haveria arquivo nenhum.
        arqs = glob.glob(os.path.join(lock, "*.lock"))
        visto["arqs_no_spawn"] = list(arqs)
        if arqs:
            with open(arqs[0]) as f:
                visto["conteudo_no_spawn"] = json.load(f)
        return FakeProc()

    ex = captura.LocalExecutor([_hot(C1, "conta-A")], motor_python=PY, motor_dir=DIR,
                               spawn=spawn_que_espia, lock_dir=lock,
                               pid_vivo=lambda p: True)
    ex.disparar(C1)
    # DENTES: sem o lock de intenção ANTES do spawn, `arqs_no_spawn` estaria vazio.
    assert visto["arqs_no_spawn"], "lock de INTENÇÃO tem de existir em disco ANTES do spawn"
    assert visto["conteudo_no_spawn"]["pid"] is None       # é INTENÇÃO (pid ainda desconhecido)
    assert visto["conteudo_no_spawn"]["course_url"] == C1
    # e APÓS o spawn o mesmo lock foi atualizado com o PID real do processo de captura.
    with open(visto["arqs_no_spawn"][0]) as f:
        final = json.load(f)
    assert final["pid"] is not None and final["course_url"] == C1


def test_restart_apos_crash_na_janela_do_toctou_NAO_redispara(tmp_path):
    # Modela o ARTEFATO que um crash na janela intenção->pid deixa em disco: um lock de
    # INTENÇÃO órfão (pid=None). A captura órfã (start_new_session) segue viva, mas seu
    # PID não chegou a ser gravado. O restart NÃO pode re-disparar essa conta.
    lock = str(tmp_path)
    semente = _exec(_hot(C1, "conta-A"), _hot(C2, "conta-A"), lock_dir=lock)
    semente._escrever_lock("conta-A", C1, None)            # lock de INTENÇÃO órfão (crash)

    # RESTART: novo executor. pid_vivo=False p/ TUDO -> prova que o lock de intenção
    # (pid=None) é fail-closed SEM depender de sondar PID nenhum (não há PID a sondar).
    sp2 = FakeSpawn()
    ex2 = _exec(_hot(C1, "conta-A"), _hot(C2, "conta-A"), spawn=sp2, lock_dir=lock,
                pid_vivo=lambda p: False)
    with pytest.raises(captura.ContaOcupada):              # DENTES: anti-ban durável no TOCTOU
        ex2.disparar(C2)                                  # 2º curso na conta ocupada
    assert len(sp2.calls) == 0
    assert ex2.disparar(C1) == f"ja_capturando:{C1}"      # idempotente p/ o MESMO curso
    assert len(sp2.calls) == 0                             # nenhum 2º browser aberto


def test_spawn_falho_remove_o_lock_de_intencao(tmp_path):
    # Se o spawn FALHA, a intenção não virou captura -> o lock de intenção tem de ser
    # removido, senão a conta ficaria travada para sempre por um disparo que nunca houve.
    lock = str(tmp_path)

    def spawn_quebrado(cmd, *, env, cwd):
        raise OSError("spawn falhou")

    ex = captura.LocalExecutor([_hot(C1, "conta-A")], motor_python=PY, motor_dir=DIR,
                               spawn=spawn_quebrado, lock_dir=lock, pid_vivo=lambda p: True)
    with pytest.raises(OSError):
        ex.disparar(C1)
    assert ex.conta_ocupada("conta-A") is False           # DENTES: conta NÃO travada
    # e um novo disparo (spawn bom) tem de conseguir rodar de fato.
    sp_ok = FakeSpawn()
    ex2 = _exec(_hot(C1, "conta-A"), spawn=sp_ok, lock_dir=lock, pid_vivo=sp_ok.mundo.vivo)
    assert ex2.disparar(C1) == f"local_iniciada:{C1}"
    assert len(sp_ok.calls) == 1


# ==========================================================================
# achado BAIXO: o lock_dir PADRÃO tem de ser DURÁVEL (sobrevive a um REBOOT). O
# tempdir do macOS (/tmp, $TMPDIR em /var/folders) some no reboot -> o guard
# anti-ban perderia a verdade. O default tem de morar no HOME.
# ==========================================================================
def test_lock_dir_padrao_e_duravel_nao_no_tempdir():
    assert tempfile.gettempdir() not in captura._LOCK_DIR_PADRAO
    assert captura._LOCK_DIR_PADRAO.startswith(os.path.expanduser("~"))
