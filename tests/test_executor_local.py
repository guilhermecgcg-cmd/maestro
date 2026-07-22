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
STOA = "https://educacao.stoa.com.br/meus-cursos"
KAJABI = "https://nepq-training.mykajabi.com/"
PY = "/opt/aula/.venv/bin/python"
DIR = "/opt/aula"
STOA_DIR = "/opt/worktrees/adaptador-stoa"          # árvore CORRIGIDA da Stoa (worktree)


def _stoa(conta="stoa"):
    return captura.CursoLocal(url=STOA, conta=conta, plataforma="stoa")


def _kajabi(conta="kajabi"):
    return captura.CursoLocal(url=KAJABI, conta=conta, plataforma="kajabi")


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


def _pend(**kw):
    """pendencias_fn injetável: contagens FIXAS por passe (0 default). Deixa a escolha do
    passe DETERMINÍSTICA no teste (sem depender de um tracker.db real). Ex.: _pend(audio=1)
    => o único passe elegível é `audio` => o comando leva `--audio`."""
    contagem = {"base": 0, "audio": 0, "embed": 0, "nao-video": 0}
    contagem.update(kw)
    return lambda url, motor_dir: dict(contagem)


# ==========================================================================
# Comando: chama o MOTOR DIRETO (não enfileira), com Groq e cwd do motor
# ==========================================================================
def test_dispara_o_motor_direto_com_url_backend_groq_e_cwd():
    sp = FakeSpawn()
    # audio=1 fixa o passe escolhido em `audio` (determinístico) — o foco deste teste é
    # módulo/Groq/cwd, não a escolha de passe (essa tem testes próprios abaixo).
    ex = _exec(_hot(C1), spawn=sp, pendencias_fn=_pend(audio=1))
    conf = ex.disparar(C1)
    assert conf                                            # confirmação truthy
    assert len(sp.calls) == 1
    call = sp.calls[0]
    # DENTES: comando é `<motor_python> -m motor.cli <url> <flag-do-passe>` (chama o motor,
    # não INSERT). Com sem_legenda pendente o passe é `audio` => `--audio`.
    assert call["cmd"] == [PY, "-m", "motor.cli", C1, "--audio"]
    assert call["env"]["WHISPER_BACKEND"] == "groq"        # INVIOLÁVEL Groq
    assert call["cwd"] == DIR                              # cwd p/ motor.config achar .env


def test_memberkit_usa_o_modulo_proprio():
    sp = FakeSpawn()
    _exec(captura.CursoLocal(url=MK, conta="mk", plataforma="memberkit"),
          spawn=sp).disparar(MK)
    assert sp.calls[0]["cmd"] == [PY, "-m", "motor.memberkit", MK]


# ==========================================================================
# REFINO 1 — --audio SÓ no caminho HOTMART (motor.cli), e SÓ quando o passe
# `audio` é o escolhido (há sem_legenda pendente). Sem ele, uma aula Hotmart
# SEM legenda vira terminal 'sem_legenda' e NUNCA chega ao Notion. Memberkit é
# ÁUDIO-NATIVO (motor.memberkit) e NUNCA leva --audio (só tem o passe `base`).
# ==========================================================================
def test_hotmart_leva_flag_audio_quando_o_passe_audio_e_escolhido():
    sp = FakeSpawn()
    _exec(_hot(C1), spawn=sp, pendencias_fn=_pend(audio=1)).disparar(C1)
    # DENTES: com sem_legenda pendente, o passe `audio` roda `--audio` — sem ele a
    # aula Hotmart sem legenda não chega ao Notion.
    assert sp.calls[0]["cmd"] == [PY, "-m", "motor.cli", C1, "--audio"]


def test_memberkit_NAO_leva_flag_audio():
    sp = FakeSpawn()
    _exec(captura.CursoLocal(url=MK, conta="mk", plataforma="memberkit"),
          spawn=sp).disparar(MK)
    # DENTES: memberkit é áudio-nativo; --audio aqui seria flag desconhecida do
    # módulo errado. O comando NÃO pode conter --audio.
    assert "--audio" not in sp.calls[0]["cmd"]


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
    assert ex1.disparar(C1).startswith(f"local_iniciada:{C1}")
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
    assert ex2.disparar(C1).startswith(f"local_iniciada:{C1}")      # PID morto -> conta livre -> retoma
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
    assert ex2.disparar(C1).startswith(f"local_iniciada:{C1}")
    assert len(sp_ok.calls) == 1


# ==========================================================================
# STOA — o executor supervisiona a captura da Stoa: motor.stoa + Chromium ISOLADO
# (nunca channel=chrome, senão colide com o Hotmart no singleton do macOS) + perfil
# DEDICADO + STOA_URL + LESSON_TIMEOUT_S + cwd na árvore CORRIGIDA (worktree).
# ==========================================================================
def test_stoa_monta_motor_stoa_chromium_url_e_timeout():
    sp = FakeSpawn()
    ex = _exec(_stoa(), spawn=sp, motor_dir_por_plataforma={"stoa": STOA_DIR})
    conf = ex.disparar(STOA)
    assert conf
    call = sp.calls[0]
    # DENTES: módulo Stoa e a URL da conta (home do tenant) como argv.
    assert call["cmd"] == [PY, "-m", "motor.stoa", STOA]
    assert "--audio" not in call["cmd"]                    # Stoa é áudio-nativo
    env = call["env"]
    assert env["MOTOR_BROWSER"] == "chromium"              # ISOLADO — NUNCA channel=chrome
    assert env["STOA_URL"] == STOA
    assert env["CHROME_USER_DATA_DIR"] == ".chrome-profile-stoa"  # perfil dedicado
    assert env["LESSON_TIMEOUT_S"] == "1800"
    assert env["WHISPER_BACKEND"] == "groq"                # INVIOLÁVEL Groq
    assert "HEADLESS" not in env                           # HEADED (captura de áudio do player)
    # DENTES: cwd/PYTHONPATH na árvore CORRIGIDA (worktree), não em /aula — é o que
    # REUSA a sessão viva e o tracker idempotente da Stoa.
    assert call["cwd"] == STOA_DIR
    assert env["PYTHONPATH"] == STOA_DIR


def test_stoa_nunca_channel_chrome():
    # A regra anti-colisão: Stoa JAMAIS pode sair como channel=chrome (colidiria com o
    # Chrome do sistema do Hotmart). MOTOR_BROWSER tem de ser 'chromium', mesmo se um
    # valor 'chrome' vazar do ambiente/extra_env.
    sp = FakeSpawn()
    _exec(_stoa(), spawn=sp, extra_env={"MOTOR_BROWSER": "chrome"}).disparar(STOA)
    assert sp.calls[0]["env"]["MOTOR_BROWSER"] == "chromium"


def test_hotmart_nunca_vira_chromium_e_fica_channel_chrome():
    # O espelho: o Hotmart NUNCA pode virar chromium. Mesmo com MOTOR_BROWSER=chromium
    # vazando do ambiente, o executor o REMOVE -> channel=chrome (o inviolável do Hotmart).
    sp = FakeSpawn()
    _exec(_hot(C1), spawn=sp, extra_env={"MOTOR_BROWSER": "chromium"}).disparar(C1)
    assert "MOTOR_BROWSER" not in sp.calls[0]["env"]       # popado -> channel=chrome


# ==========================================================================
# KAJABI — motor.kajabi + Chromium ISOLADO + perfil dedicado + KAJABI_URL. cwd
# default (/aula), onde o motor.kajabi já vive.
# ==========================================================================
def test_kajabi_monta_motor_kajabi_chromium_e_url():
    sp = FakeSpawn()
    ex = _exec(_kajabi(), spawn=sp)                         # sem override => cwd=/aula
    ex.disparar(KAJABI)
    call = sp.calls[0]
    assert call["cmd"] == [PY, "-m", "motor.kajabi", KAJABI]
    assert "--audio" not in call["cmd"]
    env = call["env"]
    assert env["MOTOR_BROWSER"] == "chromium"
    assert env["KAJABI_URL"] == KAJABI
    assert env["CHROME_USER_DATA_DIR"] == ".chrome-profile-kajabi"
    assert env["WHISPER_BACKEND"] == "groq"
    assert "HEADLESS" not in env
    assert call["cwd"] == DIR                               # default: motor.kajabi vive em /aula


# ==========================================================================
# ANTI-BAN em PARALELO: Stoa, Kajabi e Hotmart são CONTAS distintas -> disparam no
# MESMO ciclo sem ContaOcupada (perfis Chrome distintos, Chromium isolado). A MESMA
# conta serializa (já coberto genericamente; aqui a prova cross-plataforma).
# ==========================================================================
def test_stoa_kajabi_hotmart_disparam_em_paralelo():
    sp = FakeSpawn()
    ex = _exec(_stoa(), _kajabi(), _hot(C1, "hotmart"), spawn=sp,
               motor_dir_por_plataforma={"stoa": STOA_DIR})
    ex.disparar(STOA)
    ex.disparar(KAJABI)
    ex.disparar(C1)                                        # 3 contas distintas -> paralelo
    assert len(sp.calls) == 3
    assert ex.conta_ocupada("stoa") and ex.conta_ocupada("kajabi")
    assert ex.conta_ocupada("hotmart")


def test_mesma_conta_stoa_serializa():
    sp = FakeSpawn()
    # duas 'capturas' na MESMA conta stoa (patológico, mas prova o guard 1-por-conta).
    ex = _exec(_stoa("stoa"),
               captura.CursoLocal(url=STOA + "?x", conta="stoa", plataforma="stoa"),
               spawn=sp, motor_dir_por_plataforma={"stoa": STOA_DIR})
    ex.disparar(STOA)
    with pytest.raises(captura.ContaOcupada):
        ex.disparar(STOA + "?x")
    assert len(sp.calls) == 1


# ==========================================================================
# achado BAIXO: o lock_dir PADRÃO tem de ser DURÁVEL (sobrevive a um REBOOT). O
# tempdir do macOS (/tmp, $TMPDIR em /var/folders) some no reboot -> o guard
# anti-ban perderia a verdade. O default tem de morar no HOME.
# ==========================================================================
def test_lock_dir_padrao_e_duravel_nao_no_tempdir():
    assert tempfile.gettempdir() not in captura._LOCK_DIR_PADRAO
    assert captura._LOCK_DIR_PADRAO.startswith(os.path.expanduser("~"))


# ==========================================================================
# STDERR FIADO (a causa-raiz do flap): o executor tee'a stdout+stderr do motor
# por CONTA e ANEXA o path ao óbito -> a autópsia (vigia) lê o TAIL e a causa
# deixa de ser 'desconhecida'. Teste-com-DENTES: falha contra o DEVNULL antigo.
# ==========================================================================
def test_disparar_fia_stderr_tee_por_conta_no_env_privado():
    sp = FakeSpawn()
    ex = _exec(_hot(C1, "conta-A"), spawn=sp)
    ex.disparar(C1)
    env = sp.calls[0]["env"]
    # a chave PRIVADA aponta o arquivo de tee da conta (mesmo slug do lock).
    assert env[captura._ENV_STDERR_TEE] == ex._stderr_path("conta-A")
    assert env[captura._ENV_STDERR_TEE].endswith(".err")


def test_obito_carrega_stderr_path_e_vigia_le_o_tail(tmp_path):
    from maestro import vigia
    logdir = tmp_path / "motor-logs"
    sp = FakeSpawn()
    ex = _exec(_hot(C1, "conta-A"), spawn=sp, motor_log_dir=str(logdir))
    ex.disparar(C1)
    # o motor (dublê) escreveu no arquivo de tee da conta ANTES de morrer.
    errp = ex._stderr_path("conta-A")
    os.makedirs(os.path.dirname(errp), exist_ok=True)
    with open(errp, "w") as f:
        f.write("Traceback...\nSESSÃO MORTA: cookie TGC expirou\n")
    sp.procs[0].encerrar(3)                                 # SessionDead (exit 3)
    obitos = ex.drenar_obitos()
    assert obitos["conta-A"]["stderr_path"] == errp         # DENTES: falha no DEVNULL antigo
    # a autópsia consome o dict e LÊ o tail do arquivo -> causa vira classificável.
    obs = vigia.autopsia(str(tmp_path / "locks"), obitos, autopsia_dir=str(tmp_path / "aut"))
    assert len(obs) == 1
    assert "SESSÃO MORTA" in obs[0].stderr_tail


def test_spawn_popen_real_teea_stderr_e_popa_a_chave_do_filho(tmp_path):
    import sys
    errp = tmp_path / "logs" / "child.err"
    envdump = tmp_path / "envdump.txt"
    code = ("import sys,os;"
            "open(%r,'w').write(repr(os.environ.get('_ATHENA_MOTOR_STDERR')));"
            "sys.stderr.write('BOOM session expired\\n')" % str(envdump))
    env = dict(os.environ)
    env[captura._ENV_STDERR_TEE] = str(errp)
    proc = captura._spawn_popen([sys.executable, "-c", code], env=env, cwd=str(tmp_path))
    proc.wait(timeout=30)
    assert "BOOM session expired" in errp.read_text()       # tee funcionou
    assert envdump.read_text() == "None"                    # chave POPADA (filho não herdou)


# ==========================================================================
# CAUSA-RAIZ DO FLAP (ProcessSingleton): os 3 Memberkit (contas distintas) NÃO
# podem mais partilhar o `.chrome-profile` default com o Hotmart — cada tenant
# ganha perfil DEDICADO. Hotmart FIXA `.chrome-profile` (perfil do launcher
# manual, sessão restaura cookies do próprio perfil). Testes-com-DENTES: falham
# contra o perfil compartilhado (o bug que os matava na largada sob o daemon).
# ==========================================================================
def test_hotmart_mantem_perfil_historico_chrome_profile():
    # Hotmart conta ÚNICA: fixa `.chrome-profile` (não vira per-conta — arriscaria a
    # sessão, que restaura cookies do próprio perfil). Sozinho nele => não colide.
    sp = FakeSpawn()
    _exec(_hot(C1, "hotmart-principal"), spawn=sp).disparar(C1)
    assert sp.calls[0]["env"]["CHROME_USER_DATA_DIR"] == ".chrome-profile"


def test_memberkit_contas_distintas_perfis_distintos():
    # o EXATO cenário do flap: 3 tenants Memberkit em paralelo. Perfis TÊM de diferir.
    a = captura.CursoLocal(url="https://a.memberkit.com.br/", conta="mk-a",
                           plataforma="memberkit")
    b = captura.CursoLocal(url="https://b.memberkit.com.br/", conta="mk-b",
                           plataforma="memberkit")
    sp = FakeSpawn()
    ex = _exec(a, b, spawn=sp)
    ex.disparar(a.url)
    ex.disparar(b.url)
    p0 = sp.calls[0]["env"]["CHROME_USER_DATA_DIR"]
    p1 = sp.calls[1]["env"]["CHROME_USER_DATA_DIR"]
    assert p0 != p1                                        # DENTES: sem isto, colidem = flap
    assert p0 == ".chrome-profile-mk-a" and p1 == ".chrome-profile-mk-b"


def test_hotmart_e_memberkit_nao_partilham_perfil():
    # O cerne do fix: Hotmart e Memberkit rodam em PARALELO sem colidir no perfil.
    # Hotmart em `.chrome-profile`; Memberkit no SEU dedicado — diretórios distintos.
    sp = FakeSpawn()
    hot = _hot(C1, "hotmart-principal")
    mk = captura.CursoLocal(url=MK, conta="mk-x", plataforma="memberkit")
    ex = _exec(hot, mk, spawn=sp)
    ex.disparar(C1)
    ex.disparar(MK)
    perfis = {c["env"]["CHROME_USER_DATA_DIR"] for c in sp.calls}
    assert perfis == {".chrome-profile", ".chrome-profile-mk-x"}  # distintos: não colidem


def test_stoa_kajabi_mantem_perfil_do_spec_intacto():
    sp = FakeSpawn()
    _exec(_stoa(), spawn=sp, motor_dir_por_plataforma={"stoa": STOA_DIR}).disparar(STOA)
    assert sp.calls[0]["env"]["CHROME_USER_DATA_DIR"] == ".chrome-profile-stoa"
    sp2 = FakeSpawn()
    _exec(_kajabi(), spawn=sp2).disparar(KAJABI)
    assert sp2.calls[0]["env"]["CHROME_USER_DATA_DIR"] == ".chrome-profile-kajabi"


def test_disparar_limpa_singleton_orfao_do_perfil_da_conta(tmp_path):
    # simula um crash anterior: SingletonLock órfão no perfil (Hotmart => `.chrome-profile`).
    motor_dir = tmp_path / "aula"
    perfil = motor_dir / ".chrome-profile"
    perfil.mkdir(parents=True)
    lock = perfil / "SingletonLock"
    lock.write_text("stale")
    sp = FakeSpawn()
    ex = captura.LocalExecutor(
        [_hot(C1, "hotmart-principal")], motor_python=PY, motor_dir=str(motor_dir),
        spawn=sp, lock_dir=str(tmp_path / "locks"), pid_vivo=sp.mundo.vivo,
        motor_log_dir=str(tmp_path / "logs"))
    ex.disparar(C1)
    assert not lock.exists()                              # DENTES: lock órfão foi removido


# ==========================================================================
# RODÍZIO DE PASSES (correção de fiação — prioridade 1). O passe é escolhido
# POR DEMANDA (o que tem pendência no tracker), 1 passe por disparo, com anel
# anti-fome. O anti-ban NUNCA cede: 1 motor por conta por vez (o argv muda; a
# QUANTIDADE de spawns, não). Cobre os 3 dentes exigidos:
#   (i)  conta com sem_video roda --embed;
#   (ii) conta com pendente roda base;
#   (iii) NUNCA 2 passes na mesma conta no mesmo ciclo.
# ==========================================================================
def test_passe_embed_quando_conta_tem_sem_video():
    """(i) DENTE: sem_video pendente => passe `embed` => `--embed`. Sem isto as 279
    aulas sem_video jamais eram re-selecionadas (o daemon só chamava --audio)."""
    sp = FakeSpawn()
    conf = _exec(_hot(C1), spawn=sp, pendencias_fn=_pend(embed=3)).disparar(C1)
    assert sp.calls[0]["cmd"] == [PY, "-m", "motor.cli", C1, "--embed"]
    assert conf.endswith(":passe=embed")


def test_passe_base_quando_conta_tem_pendente():
    """(ii) DENTE: pendente => passe `base` (SEM flag). Sem isto as 95 aulas
    pendente jamais eram re-selecionadas (o daemon fixava --audio)."""
    sp = FakeSpawn()
    conf = _exec(_hot(C1), spawn=sp, pendencias_fn=_pend(base=5)).disparar(C1)
    assert sp.calls[0]["cmd"] == [PY, "-m", "motor.cli", C1]     # base = SEM flag
    assert conf.endswith(":passe=base")


def test_anti_ban_nunca_dois_passes_na_mesma_conta_no_mesmo_ciclo():
    """(iii) DENTE ANTI-BAN: mesmo com pendência em VÁRIOS passes (base E embed),
    o 2º disparo na MESMA conta enquanto o 1º motor vive é RECUSADO (ContaOcupada)
    — só 1 motor por conta. Se o rodízio pudesse spawnar um 2º passe em paralelo na
    conta, este teste falharia (2 sp.calls)."""
    sp = FakeSpawn()
    ex = _exec(_hot(C1, "a"), _hot(C2, "a"), spawn=sp,
               pendencias_fn=_pend(base=5, embed=5))
    ex.disparar(C1)                                       # passe 1 dispara na conta "a"
    with pytest.raises(captura.ContaOcupada):
        ex.disparar(C2)                                   # C2 é a MESMA conta "a"
    assert len(sp.calls) == 1                             # DENTE: 1 único motor na conta


def test_rodizio_alterna_entre_passes_elegiveis_entre_ciclos():
    """ANTI-FOME: com base E embed elegíveis, disparos sucessivos (conta liberando
    entre eles) ALTERNAM os passes em vez de fixar sempre o 1º — senão um backlog
    grande num passe mataria de fome os outros."""
    sp = FakeSpawn()
    ex = _exec(_hot(C1), spawn=sp, pendencias_fn=_pend(base=5, embed=5))
    escolhidos = []
    for _ in range(3):
        conf = ex.disparar(C1)
        escolhidos.append(conf.rsplit("=", 1)[1])
        sp.calls[-1]["proc"].encerrar(0)                  # encerra p/ liberar a conta
        ex._reap()
    assert escolhidos[0] != escolhidos[1]                 # alternou (anti-fome)
    assert set(escolhidos) <= {"base", "embed"}


def test_pendencias_ilegivel_cai_em_rodizio_cego_e_ainda_dispara():
    """FAIL-OPEN p/ RODÍZIO (nunca p/ paralelismo): se a leitura do tracker falha
    (None), o executor ainda DISPARA um passe cego — não trava a captura por não
    conseguir contar. 1 spawn (o anti-ban segue intacto)."""
    sp = FakeSpawn()
    conf = _exec(_hot(C1), spawn=sp, pendencias_fn=lambda u, d: None).disparar(C1)
    assert len(sp.calls) == 1
    assert conf.startswith(f"local_iniciada:{C1}:passe=")


def test_nao_video_desligado_por_default_nao_dispara_a_flag(monkeypatch):
    """nao-video fica DESLIGADO por default (braço doc->Notion é a remediação #2,
    ainda inexistente): mesmo com sem_embed pendente, NÃO dispara --nao-video — cai
    na sonda `base`. Evita saída-limpa-sem-avanço + cooldown de 6h travando o curso."""
    monkeypatch.delenv("ATHENA_NAO_VIDEO_ATIVO", raising=False)
    sp = FakeSpawn()
    conf = _exec(_hot(C1), spawn=sp,
                 pendencias_fn=_pend(**{"nao-video": 4})).disparar(C1)
    assert "--nao-video" not in sp.calls[0]["cmd"]
    assert conf.endswith(":passe=base")


def test_nao_video_ligado_por_env_dispara_a_flag(monkeypatch):
    """Quando a remediação #2 entregar o braço, `ATHENA_NAO_VIDEO_ATIVO=1` liga o
    passe SEM tocar código: aí sem_embed pendente roda `--nao-video`."""
    monkeypatch.setenv("ATHENA_NAO_VIDEO_ATIVO", "1")
    sp = FakeSpawn()
    conf = _exec(_hot(C1), spawn=sp,
                 pendencias_fn=_pend(**{"nao-video": 4})).disparar(C1)
    assert sp.calls[0]["cmd"] == [PY, "-m", "motor.cli", C1, "--nao-video"]
    assert conf.endswith(":passe=nao-video")


def test_memberkit_passe_unico_ignora_pendencias_e_nunca_leva_flag():
    """Plataforma áudio-nativa (passes=("base",)): a escolha de passe é no-op —
    NUNCA consulta pendências nem anexa flag (passá-la seria argumento desconhecido
    do módulo). pendencias_fn que EXPLODE prova que nem é chamado."""
    def _boom(url, motor_dir):
        raise AssertionError("passe único não pode consultar pendências")
    sp = FakeSpawn()
    _exec(captura.CursoLocal(url=MK, conta="mk", plataforma="memberkit"),
          spawn=sp, pendencias_fn=_boom).disparar(MK)
    assert sp.calls[0]["cmd"] == [PY, "-m", "motor.memberkit", MK]


def test_pendencias_tracker_conta_por_course_id_da_url(tmp_path):
    """O leitor default resolve o course_id pelo /products/<id> da URL (a coluna
    courses.url é stale) e BUCKETIZA os status nos passes, isolando outros cursos e
    excluindo terminais (no_notion). É o path de PRODUÇÃO — testado contra o schema
    real (SELECT status,COUNT(*) ... GROUP BY status)."""
    import sqlite3
    db = tmp_path / "tracker.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE lessons(hash TEXT PRIMARY KEY, course_id TEXT, status TEXT)")
    linhas = [
        ("h1", "5431484", "sem_video"), ("h2", "5431484", "sem_video"),
        ("h3", "5431484", "transcrevendo_embed"),          # também é `embed`
        ("h4", "5431484", "pendente"),                     # `base`
        ("h5", "5431484", "falhou"),                       # `base`
        ("h6", "5431484", "sem_embed"),                    # `nao-video`
        ("h7", "5431484", "no_notion"),                    # terminal: fora de tudo
        ("h8", "9999", "sem_video"),                       # OUTRO curso: ignorado
    ]
    con.executemany("INSERT INTO lessons VALUES(?,?,?)", linhas)
    con.commit()
    con.close()
    pend = captura._pendencias_tracker(
        "https://hotmart.com/pt-br/club/x/products/5431484", str(tmp_path))
    assert pend == {"base": 2, "audio": 0, "embed": 3, "nao-video": 1}


def test_pendencias_tracker_none_fail_open(tmp_path):
    """None (=> rodízio cego) quando: db ausente, ou a URL não tem /products/<id>."""
    assert captura._pendencias_tracker("http://x/products/1", str(tmp_path)) is None
    # cria db vazio mas passa URL sem product-id => course_id não resolve => None
    (tmp_path / "tracker.db").write_bytes(b"")
    assert captura._pendencias_tracker("http://x/sem-produto", str(tmp_path)) is None


# ==========================================================================
# RODÍZIO DE PASSES DA STOA (ativação das 3 ferramentas). Espelha o rodízio
# do Hotmart: 1 passe por disparo, escolhido por demanda, anel anti-fome —
# e o ANTI-BAN nunca cede (1 motor por conta; o rodízio muda só o argv).
# Os passes novos (embed/nao-video) são GATED por ATHENA_STOA_PASSES_ATIVO:
# desligado => comportamento vivo de hoje (só base áudio-nativo).
# (Reusa o STOA e o helper `_stoa(conta=...)` do topo do módulo.)
# ==========================================================================
def _stoa2(url):
    """2º curso na MESMA conta stoa (patológico, p/ o dente anti-ban)."""
    return captura.CursoLocal(url=url, conta="stoa", plataforma="stoa")


def test_stoa_sem_flag_roda_so_base_mesmo_com_sem_audio_pendente(monkeypatch):
    """GATE: sem ATHENA_STOA_PASSES_ATIVO, a Stoa NÃO dispara --embed/--nao-video
    mesmo com 178 sem_audio pendentes — cai na sonda `base` (o vivo de hoje).
    A ativação é deliberada (flag + restart guardado), nunca acidental."""
    monkeypatch.delenv("ATHENA_STOA_PASSES_ATIVO", raising=False)
    sp = FakeSpawn()
    conf = _exec(_stoa(), spawn=sp,
                 pendencias_fn=_pend(embed=178, **{"nao-video": 4})).disparar(STOA)
    assert sp.calls[0]["cmd"] == [PY, "-m", "motor.stoa", STOA]   # base = SEM flag
    assert conf.endswith(":passe=base")


def test_stoa_com_flag_dispara_embed_quando_ha_sem_audio(monkeypatch):
    """DENTE: com a flag ligada e sem_audio pendente, o passe `embed` roda `--embed`
    — é o que torna as 178 sem_audio re-selecionáveis (o motor separa embed-externo
    de doc/texto)."""
    monkeypatch.setenv("ATHENA_STOA_PASSES_ATIVO", "1")
    sp = FakeSpawn()
    conf = _exec(_stoa(), spawn=sp, pendencias_fn=_pend(embed=178)).disparar(STOA)
    assert sp.calls[0]["cmd"] == [PY, "-m", "motor.stoa", STOA, "--embed"]
    assert conf.endswith(":passe=embed")


def test_stoa_com_flag_dispara_nao_video_quando_ha_sem_embed(monkeypatch):
    """DENTE: com a flag ligada e sem_embed pendente (docs confirmados pelo --embed),
    o passe `nao-video` roda `--nao-video` (doc→Notion)."""
    monkeypatch.setenv("ATHENA_STOA_PASSES_ATIVO", "1")
    sp = FakeSpawn()
    conf = _exec(_stoa(), spawn=sp,
                 pendencias_fn=_pend(**{"nao-video": 4})).disparar(STOA)
    assert sp.calls[0]["cmd"] == [PY, "-m", "motor.stoa", STOA, "--nao-video"]
    assert conf.endswith(":passe=nao-video")


def test_stoa_rodizio_alterna_entre_passes_elegiveis(monkeypatch):
    """ANTI-FOME: base E embed elegíveis => disparos sucessivos ALTERNAM (senão o
    backlog de 178 sem_audio mataria de fome o base, ou vice-versa)."""
    monkeypatch.setenv("ATHENA_STOA_PASSES_ATIVO", "1")
    sp = FakeSpawn()
    ex = _exec(_stoa(), spawn=sp, pendencias_fn=_pend(base=5, embed=178))
    escolhidos = []
    for _ in range(3):
        conf = ex.disparar(STOA)
        escolhidos.append(conf.rsplit("=", 1)[1])
        sp.calls[-1]["proc"].encerrar(0)                  # encerra p/ liberar a conta
        ex._reap()
    assert escolhidos[0] != escolhidos[1]                 # alternou (anti-fome)
    assert set(escolhidos) <= {"base", "embed"}


def test_stoa_anti_ban_nunca_dois_motores_na_mesma_conta(monkeypatch):
    """DENTE ANTI-BAN (INVIOLÁVEL): com pendência em vários passes, um 2º disparo na
    conta stoa-principal enquanto o 1º motor vive é RECUSADO (ContaOcupada). O
    rodízio jamais spawna um 2º passe em paralelo na Stoa."""
    monkeypatch.setenv("ATHENA_STOA_PASSES_ATIVO", "1")
    sp = FakeSpawn()
    ex = _exec(_stoa2(STOA), _stoa2(STOA + "?b"), spawn=sp,
               pendencias_fn=_pend(base=5, embed=178, **{"nao-video": 4}))
    ex.disparar(STOA)
    with pytest.raises(captura.ContaOcupada):
        ex.disparar(STOA + "?b")                          # MESMA conta stoa-principal
    assert len(sp.calls) == 1                             # 1 único motor na conta


def test_stoa_idempotente_mesmo_curso_nao_respawna(monkeypatch):
    """IDEMPOTÊNCIA: re-disparar o MESMO curso Stoa com o motor vivo devolve
    ja_capturando sem abrir 2º processo (mesmo contrato do Hotmart)."""
    monkeypatch.setenv("ATHENA_STOA_PASSES_ATIVO", "1")
    sp = FakeSpawn()
    ex = _exec(_stoa(), spawn=sp, pendencias_fn=_pend(embed=178))
    ex.disparar(STOA)
    assert ex.disparar(STOA) == f"ja_capturando:{STOA}"
    assert len(sp.calls) == 1


def test_pendencias_tracker_stoa_agrega_o_tenant_inteiro(tmp_path):
    """O leitor default da Stoa agrega TODOS os course_ids do tracker (curso-único no
    daemon; a URL não tem /products/) e bucketiza pelos pools PRÓPRIOS da Stoa:
    sem_audio => embed (as 178), sem_embed => nao-video, pendente/transcrevendo =>
    base. Terminais (no_notion/audio_erro/sem_conteudo) fora de tudo."""
    import sqlite3
    con = sqlite3.connect(tmp_path / "tracker.db")
    con.execute("CREATE TABLE lessons(hash TEXT PRIMARY KEY, course_id TEXT, status TEXT)")
    linhas = [
        ("h1", "stoa:educacao:9227", "sem_audio"),         # embed (candidata)
        ("h2", "stoa:educacao:10389", "sem_audio"),        # embed (OUTRO curso: conta)
        ("h3", "stoa:educacao:9227", "transcrevendo_embed"),  # embed (resume)
        ("h4", "stoa:educacao:9224", "pendente"),          # base
        ("h5", "stoa:educacao:9224", "transcrevendo"),     # base (resume)
        ("h6", "stoa:educacao:10102", "sem_embed"),        # nao-video
        ("h7", "stoa:educacao:9227", "no_notion"),         # terminal: fora
        ("h8", "stoa:educacao:9227", "audio_erro"),        # terminal: fora
        # DEFESA EM PROFUNDIDADE (achado do review): linhas de OUTRA plataforma no
        # mesmo db (o fallback de motor_dir cairia no tracker do Hotmart) NÃO podem
        # contaminar as contagens da Stoa — o filtro course_id LIKE 'stoa:%' exclui.
        ("h9", "5431484", "sem_audio"),                    # Hotmart: fora
        ("hA", "5431484", "sem_embed"),                    # Hotmart: fora
    ]
    con.executemany("INSERT INTO lessons VALUES(?,?,?)", linhas)
    con.commit()
    con.close()
    pend = captura._pendencias_tracker_stoa(STOA, str(tmp_path))
    assert pend == {"base": 2, "embed": 3, "nao-video": 1}


def test_stoa_gate_desligado_nem_consulta_pendencias(monkeypatch):
    """Com o gate desligado só existe 1 passe ativável (base): a escolha está
    decidida e o executor NEM consulta pendências (caminho de execução idêntico
    ao de antes da fiação multi-passe; achado do review). Um GRAVADOR de chamadas
    prova que o leitor não é invocado (levantar não serviria de dente: o
    `_escolher_passe` engole exceção do leitor como fail-open p/ rodízio cego)."""
    monkeypatch.delenv("ATHENA_STOA_PASSES_ATIVO", raising=False)
    chamadas = []
    def _gravador(url, motor_dir):
        chamadas.append(url)
        return {"base": 0, "embed": 178, "nao-video": 0}
    sp = FakeSpawn()
    conf = _exec(_stoa(), spawn=sp, pendencias_fn=_gravador).disparar(STOA)
    assert chamadas == []                                 # DENTE: leitor nem chamado
    assert sp.calls[0]["cmd"] == [PY, "-m", "motor.stoa", STOA]
    assert conf.endswith(":passe=base")


def test_pendencias_tracker_stoa_none_fail_open(tmp_path):
    """None (=> rodízio cego) quando o db não existe — fail-open p/ RODÍZIO, nunca
    p/ paralelismo (o anti-ban é o lock durável, não passa por aqui)."""
    assert captura._pendencias_tracker_stoa(STOA, str(tmp_path)) is None


def test_stoa_default_sem_injecao_usa_o_leitor_proprio(tmp_path, monkeypatch):
    """FIAÇÃO DEFAULT: sem pendencias_fn injetado, a plataforma stoa resolve o leitor
    PRÓPRIO (agregado) — não o do Hotmart (que devolveria None pela URL sem /products/
    e cairia no rodízio cego). Com só sem_audio no tracker e a flag ligada, o disparo
    real vai de `--embed` (determinístico, não cego)."""
    monkeypatch.setenv("ATHENA_STOA_PASSES_ATIVO", "1")
    import sqlite3
    con = sqlite3.connect(tmp_path / "tracker.db")
    con.execute("CREATE TABLE lessons(hash TEXT PRIMARY KEY, course_id TEXT, status TEXT)")
    con.execute("INSERT INTO lessons VALUES('h1','stoa:educacao:9227','sem_audio')")
    con.commit()
    con.close()
    sp = FakeSpawn()
    ex = _exec(_stoa(), spawn=sp,
               motor_dir_por_plataforma={"stoa": str(tmp_path)})
    conf = ex.disparar(STOA)
    assert sp.calls[0]["cmd"] == [PY, "-m", "motor.stoa", STOA, "--embed"]
    assert sp.calls[0]["cwd"] == str(tmp_path)            # cwd = worktree da Stoa
    assert conf.endswith(":passe=embed")
