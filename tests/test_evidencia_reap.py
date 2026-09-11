"""EVIDÊNCIA DA AUTÓPSIA SOBRESCRITA (incidente 10/09 23:33–23:55).

O MECANISMO REAL (conferido nas autópsias em ~/.athena-local/autopsias, só leitura):
  - o `_spawn_popen` abre o .err da CONTA com "wb" a cada disparo (TRUNCA);
  - o `_reap` colhia o óbito guardando SÓ o `stderr_path`; o vigia lia o TAIL depois;
  - o `_reap` roda em QUALQUER consulta ao executor (curso_ativo/conta_ocupada/disparar,
    o pulso cheio) — inclusive NO MEIO da passada, depois da autópsia do ciclo;
  - entre esse reap e a autópsia do ciclo SEGUINTE a conta era RELANÇADA (mesmo curso
    ou outro curso da mesma conta) e o .err truncado pelo run novo.
Resultado provado: a autópsia 20260910T234637 kiwify-principal (exit 1) tem no
stderr_tail as linhas do run SEGUINTE (disparado 23:44:32, linhas 23:45:29..23:46:05);
o TimeoutError real sumiu -> "causa desconhecida" -> escalar_humano -> disjuntor.
O mesmo nas de 23:35:57 (hubla/kajabi/memberkit: cauda VAZIA do run recém-truncado)
e na de 23:51:11 stoa-principal (cauda = enumeração do run disparado 23:49:06).

DUBLÊ COM DENTES: `_SpawnTee` modela o `_spawn_popen` no que importa — TRUNCA o .err
da conta a cada disparo e o run escreve nele. O dublê antigo (FakeExecutorObitos) só
matava filhos ENTRE ciclos e entregava `stderr_tail` pronto: nunca exercitava o reap
no meio da passada nem o truncamento — por isso a suíte verde não viu o bug.
"""
import os

from maestro import athena_local, causa, disjuntor, vigia
from maestro.adaptadores import captura

PY = "/opt/aula/.venv/bin/python"
DIR = "/opt/aula"
C1 = "https://hotmart.com/pt-br/club/x/products/111"
C2 = "https://hotmart.com/pt-br/club/y/products/222"
KIWIFY = "https://dashboard.kiwify.com.br/courses"

# Cauda REAL de uma morte por timeout de navegação sob a carga do incidente (autópsia
# 20260910T234637 kajabi-principal — a ÚNICA daquele lote cujo .err não foi truncado a
# tempo). É o que o motor da kiwify-principal também imprimiu e a autópsia perdeu.
_RUN_MORREU_DE_TIMEOUT = (
    "2026-09-10 23:34:02,118 INFO motor.kiwify.session: kiwify: injetados 6 cookies do "
    "storage_state salvo (reforço)\n"
    "Traceback (most recent call last):\n"
    '  File "/Users/guilhermerodrigues/teste/aula/motor/kajabi/session.py", line 102, in '
    "probe_session\n"
    '    await page.goto(library_url(home_url), wait_until="domcontentloaded")\n'
    '  File "/Users/guilhermerodrigues/teste/aula/.venv/lib/python3.11/site-packages/'
    'playwright/_impl/_connection.py", line 563, in wrap_api_call\n'
    "    raise rewrite_error(error, f\"{parsed_st['apiName']}: {error}\") from None\n"
    "playwright._impl._errors.TimeoutError: Page.goto: Timeout 30000ms exceeded.\n"
    "Call log:\n"
    '  - navigating to "https://nepq-training.mykajabi.com/library", waiting until '
    '"domcontentloaded"\n')

# As linhas REAIS do run SEGUINTE que a autópsia 20260910T234637 kiwify-principal leu
# no lugar da morte (disparado 23:44:32). Nenhuma assinatura: "causa desconhecida".
_RUN_SEGUINTE = (
    "2026-09-10 23:45:29,792 INFO motor.kiwify.session: kiwify: injetados 6 cookies do "
    "storage_state salvo (reforço)\n"
    "2026-09-10 23:46:01,970 INFO motor.kiwify.session: sessão Kiwify viva\n"
    "2026-09-10 23:46:05,327 INFO motor.hotmart.session: sessão persistida em "
    ".kiwify-session.json (6 cookies)\n")


class _Proc:
    """Popen mínimo: vivo (poll()->None) até `encerrar(code)`; pid único e estável."""
    _seq = 71000

    def __init__(self):
        _Proc._seq += 1
        self.pid = _Proc._seq
        self.returncode = None

    def poll(self):
        return self.returncode

    def encerrar(self, code):
        self.returncode = code


class _SpawnTee:
    """Modela o `_spawn_popen` REAL: a cada disparo ABRE o .err da conta com "wb"
    (TRUNCA — a linha `open(err_path, "wb")`) e o run escreve nele a sua saída. A tabela
    de processos (`vivo`) é o dublê do `os.kill(pid, 0)`."""
    def __init__(self, saidas):
        self.saidas = list(saidas)
        self.calls = []
        self.procs = {}

    def __call__(self, cmd, *, env, cwd):
        err = env[captura._ENV_STDERR_TEE]
        os.makedirs(os.path.dirname(err), exist_ok=True)
        with open(err, "wb") as f:                       # TRUNCA como o spawn real
            if self.saidas:
                f.write(self.saidas.pop(0).encode("utf-8"))
        proc = _Proc()
        self.procs[proc.pid] = proc
        self.calls.append({"cmd": cmd, "proc": proc})
        return proc

    def vivo(self, pid):
        p = self.procs.get(pid)
        return p is not None and p.poll() is None


class _VigiaNoMundo:
    """O vigia REAL, sondando a MESMA tabela de processos do executor (o `os.kill` real
    veria os PIDs de mentira como mortos e inventaria mortes só-de-lock)."""
    def __init__(self, vivo):
        self._vivo = vivo

    def autopsia(self, lock_dir, fontes, **kw):
        return vigia.autopsia(lock_dir, fontes, pid_vivo=self._vivo, **kw)


def _executor(tmp_path, cursos, spawn):
    return captura.LocalExecutor(
        cursos, motor_python=PY, motor_dir=DIR, spawn=spawn,
        lock_dir=str(tmp_path / "locks"), motor_log_dir=str(tmp_path / "logs"),
        pid_vivo=spawn.vivo, pendencias_fn=lambda u, d: None)


# ==========================================================================
# 1) NÍVEL EXECUTOR: reap -> relançamento trunca o .err -> a autópsia AINDA vê o
#    TimeoutError da morte e a causa decide RELANÇAR (não "causa desconhecida").
# ==========================================================================
def test_reap_guarda_a_cauda_antes_do_relancamento_truncar_o_err(tmp_path):
    sp = _SpawnTee([_RUN_MORREU_DE_TIMEOUT, _RUN_SEGUINTE])
    ex = _executor(tmp_path, [captura.CursoLocal(KIWIFY, "kiwify-principal", "kiwify")],
                   sp)
    ex.disparar(KIWIFY)
    sp.calls[0]["proc"].encerrar(1)                       # morreu: TimeoutError, exit 1
    assert ex.curso_ativo(KIWIFY) is False                # COLHIDO aqui (meio da passada)
    ex.disparar(KIWIFY)                                   # relançado ANTES da autópsia
    err = ex._stderr_path("kiwify-principal")
    assert "TimeoutError" not in open(err).read()         # o mecanismo: o .err é do run NOVO
    obitos = ex.drenar_obitos()
    [ob] = vigia.autopsia(str(tmp_path / "locks"), obitos, pid_vivo=sp.vivo,
                          autopsia_dir=str(tmp_path / "aut"),
                          stderr_path_de=ex._stderr_path, boot_ts=0.0)
    assert ob.exit_code == 1
    # DENTES: no código antigo o óbito só tinha o path -> o vigia lia o run SEGUINTE.
    assert "TimeoutError: Page.goto: Timeout 30000ms exceeded" in ob.stderr_tail
    assert "sessão Kiwify viva" not in ob.stderr_tail     # nada do run seguinte
    dec = causa.classificar(ob)
    assert dec.acao == "relancar" and "timeout" in dec.motivo, dec


def test_cauda_de_outro_run_nao_vira_evidencia_quando_o_tee_falhou(tmp_path):
    # Se o tee falhou NESTE disparo (o `_spawn_popen` cai no DEVNULL), o .err no disco é
    # de um run ANTERIOR: não é a evidência desta morte. Honesto = cauda vazia (a causa
    # segue fail-closed), jamais a de outro run lida como se fosse desta.
    class _SpawnSemTee(_SpawnTee):
        def __call__(self, cmd, *, env, cwd):            # não abre o .err (tee falhou)
            proc = _Proc()
            self.procs[proc.pid] = proc
            self.calls.append({"cmd": cmd, "proc": proc})
            return proc

    sp = _SpawnSemTee([])
    ex = _executor(tmp_path, [captura.CursoLocal(KIWIFY, "k", "kiwify")], sp)
    err = ex._stderr_path("k")
    os.makedirs(os.path.dirname(err), exist_ok=True)
    with open(err, "w") as f:
        f.write(_RUN_MORREU_DE_TIMEOUT)                   # sobra de um run de ONTEM
    velho = os.path.getmtime(err) - 86400
    os.utime(err, (velho, velho))
    ex.disparar(KIWIFY)
    sp.calls[0]["proc"].encerrar(1)
    obitos = ex.drenar_obitos()
    assert obitos["k"]["stderr_tail"] == ""               # não atribui o run de ontem


def test_obito_de_saida_limpa_tambem_leva_a_cauda_do_proprio_run(tmp_path):
    # exit 0: a cauda (o resumo final) também é capturada no reap — a causa usa a do
    # PRÓPRIO run mesmo que outro curso da conta já tenha truncado o .err.
    sp = _SpawnTee(["Stats: total=2 ok=2 audio=0 falhou=0\n", _RUN_SEGUINTE])
    cursos = [captura.CursoLocal(C1, "a", "hotmart"), captura.CursoLocal(C2, "a", "hotmart")]
    ex = _executor(tmp_path, cursos, sp)
    ex.disparar(C1)
    sp.calls[0]["proc"].encerrar(0)
    ex.disparar(C2)                                       # colhe C1 e trunca o .err da conta
    obitos = ex.drenar_obitos()
    assert obitos["a"]["curso"] == C1 and obitos["a"]["exit_code"] == 0
    assert "Stats: total=2" in obitos["a"]["stderr_tail"]


# ==========================================================================
# 2) NÍVEL LOOP (ciclo_local + LocalExecutor REAL + vigia/causa/disjuntor REAIS): a
#    sequência do incidente. O motor morre ENQUANTO o loop lê o Notion (a passada), é
#    colhido ali, a conta é re-disparada no MESMO ciclo (outro curso da conta) e só no
#    ciclo seguinte a autópsia roda. Tem de classificar pelo TimeoutError (relancar,
#    sem alerta essencial) — não "causa desconhecida (fail-closed)".
# ==========================================================================
class _Voz:
    def __init__(self):
        self.escaladas = []

    def avisar_acao(self, acao):
        pass

    def escalar(self, problema, pedido):
        self.escaladas.append((problema, pedido))


class _Alertas:
    def __init__(self):
        self.mortes = []

    def captura_morreu(self, plataforma, motivo, **kw):
        self.mortes.append((motivo, kw))

    def sessao_expirada(self, plataforma, **kw):
        self.mortes.append(("SESSAO", {"essencial": True}))

    def curso_concluido(self, *a, **kw):
        pass

    def essenciais(self):
        return [m for m in self.mortes if m[1].get("essencial")]


def test_ciclo_morte_colhida_na_passada_e_relancada_antes_da_autopsia(tmp_path, monkeypatch):
    # ISOLA o item 1: a passada de hoje também ESPERA a autópsia quando a conta tem óbito
    # pendente (tests/test_passada_aguarda_autopsia.py) — defesa em profundidade. Aqui ela
    # é desligada para reproduzir o loop do incidente, em que a conta ERA relançada antes
    # da autópsia: mesmo assim a evidência tem de sobreviver (dente só do item 1).
    monkeypatch.setattr(athena_local, "_aguardando_autopsia", lambda ex, curso: False)
    sp = _SpawnTee([_RUN_MORREU_DE_TIMEOUT, _RUN_SEGUINTE])
    cursos = [captura.CursoLocal(C1, "hotmart-principal", "hotmart", total_esperado=18),
              captura.CursoLocal(C2, "hotmart-principal", "hotmart", total_esperado=18)]
    ex = _executor(tmp_path, cursos, sp)
    morrer_ao_ler_o_notion = set()

    def prog(curso):
        # a leitura do Notion é LENTA sob carga (até 120s): o motor morre nesse meio-tempo
        if curso in morrer_ao_ler_o_notion:
            morrer_ao_ler_o_notion.discard(curso)
            sp.calls[0]["proc"].encerrar(1)
        return (0, 18)

    alertas, voz, estado, voo = _Alertas(), _Voz(), {}, {}
    kw = dict(disjuntor=disjuntor, vigia=_VigiaNoMundo(sp.vivo), causa=causa, alertas=alertas,
              lock_dir=str(tmp_path / "locks"), autopsia_dir=str(tmp_path / "aut"),
              boot_ts=0.0)
    athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=1000.0, **kw)
    assert len(sp.calls) == 1                             # C1 dispara; C2 aguarda a conta
    morrer_ao_ler_o_notion.add(C1)
    athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=1200.0, **kw)
    assert len(sp.calls) == 2                             # a conta foi RE-disparada...
    assert "TimeoutError" not in open(ex._stderr_path("hotmart-principal")).read()
    athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=1400.0, **kw)

    import glob
    import json
    auts = [json.load(open(p)) for p in glob.glob(str(tmp_path / "aut" / "*.json"))]
    [aut] = [a for a in auts if a["curso"] == C1]
    # DENTES: contra o código antigo -> cauda = run seguinte, acao escalar_humano
    # (fail-closed "causa desconhecida e nenhum LLM disponível") + alerta ESSENCIAL.
    assert "TimeoutError" in aut["stderr_tail"], aut["stderr_tail"]
    assert aut["acao"] == "relancar" and "timeout" in aut["motivo"], aut
    assert alertas.essenciais() == [], alertas.mortes
