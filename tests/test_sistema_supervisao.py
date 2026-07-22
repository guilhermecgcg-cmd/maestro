"""F4-d: DENTES da supervisão de SISTEMAS GERADOS como alvos ao lado da captura.

Três frentes:
  A. `causa_sistema` — conjunto-irmão FECHADO: SEM `escalar_reseed`, com
     `acionar_engenharia`; LLM recusável; fail-closed. (Um sistema não tem sessão.)
  B. `SistemaExecutor` (espelho do LocalExecutor) sobre um PROCESSO REAL: dispara,
     sistema_ativo, e MATA um run travado com SEGURANÇA (kill — jamais reseed).
  C. `passada_sistema` — a máquina de estados: heartbeat velho → mata; óbito →
     causa_sistema → backoff/engenharia; resultado.json novo → custo por sistema
     (medido/presumido SEPARADOS). NENHUM caminho aciona reseed.
"""
import json
import os
import sys
import time

from maestro import athena_local, causa_sistema, disjuntor, vigia
from maestro.adaptadores.sistema import SistemaExecutor, SistemaSpec, SistemaOcupado


# ==========================================================================
# A. causa_sistema — conjunto FECHADO, SEM reseed
# ==========================================================================
class _Ob:
    def __init__(self, exit_code=None, stderr_tail="", conta="sX"):
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail
        self.conta = conta
        self.curso = "/raiz"
        self.flaps_na_janela = 1


def test_causa_sistema_nunca_tem_reseed():
    assert "escalar_reseed" not in causa_sistema.ACOES
    assert "relogar" not in causa_sistema.ACOES


def test_causa_sistema_exit_codes_deterministicos():
    assert causa_sistema.classificar(_Ob(exit_code=30)).acao == "acionar_engenharia"
    assert causa_sistema.classificar(_Ob(exit_code=40)).acao == "acionar_engenharia"
    assert causa_sistema.classificar(_Ob(exit_code=-9)).acao == "relancar"
    assert causa_sistema.classificar(_Ob(exit_code=0)).acao == "nada"
    assert causa_sistema.classificar(_Ob(exit_code=10)).acao == "nada"
    assert causa_sistema.classificar(_Ob(exit_code=20)).acao == "nada"


def test_causa_sistema_token_e_desconhecida():
    d = causa_sistema.classificar(_Ob(exit_code=1, stderr_tail="HTTP 401 Unauthorized"))
    assert d.acao == "escalar_token"
    # desconhecida + sem LLM → fail-closed escalar_humano (nunca chuta).
    d2 = causa_sistema.classificar(_Ob(exit_code=1, stderr_tail="algo estranho aqui"))
    assert d2.acao == "escalar_humano" and d2.fonte == "fail-closed"


def test_causa_sistema_llm_reseed_e_recusado():
    """INVIOLÁVEL: mesmo bem-formatado, um LLM que proponha reseed/login é RECUSADO
    → vira escalar_humano. Um sistema não tem sessão a refazer."""
    llm_reseed = lambda p: '{"acao": "escalar_reseed"}'
    d = causa_sistema.classificar(_Ob(exit_code=1, stderr_tail="???"), llm=llm_reseed)
    assert d.acao == "escalar_humano"
    # já uma ação válida do conjunto é aceita.
    llm_ok = lambda p: '{"acao": "pausar_sistema"}'
    d2 = causa_sistema.classificar(_Ob(exit_code=1, stderr_tail="???"), llm=llm_ok)
    assert d2.acao == "pausar_sistema" and d2.fonte == "llm"


# ==========================================================================
# B. SistemaExecutor sobre um PROCESSO REAL — kill-safe do run travado
# ==========================================================================
def _spawn_sleeper(cmd, *, env, cwd, stderr_path):
    """Injeta um processo REAL controlável no lugar do entrypoint (que precisaria de
    API). Dorme até ser morto — modela um run TRAVADO."""
    import subprocess
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def test_sistema_executor_dispara_e_mata_run_real_com_seguranca(tmp_path):
    spec = SistemaSpec(slug="dummy", raiz=str(tmp_path / "sis"))
    ex = SistemaExecutor([spec], fabrica_python=sys.executable,
                         fabrica_dir=str(tmp_path), spawn=_spawn_sleeper,
                         lock_dir=str(tmp_path / "locks-sistemas"))

    conf = ex.disparar("dummy")
    assert conf.startswith("run_iniciado:dummy")
    assert ex.sistema_ativo("dummy") is True
    # idempotente por slug: 2º disparo NÃO abre 2º processo.
    assert ex.disparar("dummy").startswith("ja_rodando:")

    # MATA o run travado — SEGURO (sistema não tem sessão/anti-ban). A captura
    # JAMAIS faria isto; aqui é a ação correta.
    assert ex.matar("dummy") is True
    assert ex.sistema_ativo("dummy") is False
    # o óbito é drenado com exit_code (processo terminado por sinal → negativo).
    obitos = ex.drenar_obitos()
    assert "dummy" in obitos
    assert obitos["dummy"]["exit_code"] is not None
    # lock removido (conta livre p/ um novo run).
    assert not os.path.exists(os.path.join(str(tmp_path / "locks-sistemas"), "dummy.lock"))


# ==========================================================================
# C. passada_sistema — a máquina de estados (com fakes controláveis)
# ==========================================================================
class FakeSistemaExecutor:
    def __init__(self, *, ativo=False, obitos=None):
        self._ativo = ativo
        self._obitos = obitos or {}
        self.matou = []
        self.disparos = []

    def sistema_ativo(self, slug):
        return self._ativo

    def drenar_obitos(self):
        out = dict(self._obitos)
        self._obitos = {}
        return out

    def matar(self, slug, **kw):
        self.matou.append(slug)
        self._ativo = False
        return True

    def disparar(self, slug):
        self.disparos.append(slug)
        return f"run_iniciado:{slug}"


class SpyEspinha:
    def __init__(self):
        self.regs = []

    def registrar_decisao(self, o_que, por_que, **kw):
        self.regs.append({"o_que": o_que, "por_que": por_que, **kw})


def _spec(tmp_path):
    return SistemaSpec(slug="sis1", raiz=str(tmp_path / "sis"))


def _escrever_heartbeat(raiz, ts):
    d = os.path.join(str(raiz), "estado")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "heartbeat.json"), "w") as f:
        json.dump({"ts": ts, "run_id": "run-x", "etapa": "e1", "pid": 999}, f)


def _kw(tmp_path):
    return dict(vigia=vigia, causa_sistema=causa_sistema, disjuntor=disjuntor,
                alertas=athena_local._AlertasNulo(),
                lock_dir=str(tmp_path / "locks-sistemas"),
                autopsia_dir=str(tmp_path / "aut-sis"), flap_min=3)


def test_passada_heartbeat_velho_mata_e_registra(tmp_path):
    spec = _spec(tmp_path)
    _escrever_heartbeat(spec.raiz, ts=0.0)              # heartbeat MUITO velho
    ex = FakeSistemaExecutor(ativo=True)
    esp = SpyEspinha()
    st = {}
    acao = athena_local.passada_sistema(
        spec, st, ex, espinha=esp, agora=100000.0, heartbeat_limiar_s=900, **_kw(tmp_path))
    assert ex.matou == ["sis1"]                         # travado → MORTO (seguro)
    assert acao is not None and acao.executada
    assert any("matei run travado" in r["o_que"] for r in esp.regs)
    # NENHUMA decisão de reseed em lugar nenhum (sistema não tem sessão).
    assert not any("reseed" in str(r).lower() for r in esp.regs)


def test_passada_obito_engenharia_backoff_sem_reseed(tmp_path):
    spec = _spec(tmp_path)
    ex = FakeSistemaExecutor(ativo=False, obitos={
        "sis1": {"conta": "sis1", "curso": spec.raiz, "exit_code": 30,
                 "stderr_tail": "", "pid": None, "stderr_path": None}})
    esp = SpyEspinha()
    st = {}
    athena_local.passada_sistema(spec, st, ex, espinha=esp, agora=1000.0, **_kw(tmp_path))
    # exit 30 → acionar_engenharia → disjuntor CONTABILIZOU a falha (backoff).
    assert st.get("disj_falhas", 0) >= 1
    assert any("aciono engenharia" in r["o_que"] for r in esp.regs)
    assert any(r.get("trava") == "executor_ausente" for r in esp.regs)
    assert not any("reseed" in str(r).lower() for r in esp.regs)


def test_passada_resultado_novo_registra_custo_por_sistema(tmp_path):
    spec = _spec(tmp_path)
    d = os.path.join(str(spec.raiz), "estado")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "resultado-run-9.json"), "w") as f:
        json.dump({"run_id": "run-9", "estado": "aprovado", "sucesso": True,
                   "custo_medido_usd": 0.42, "custo_presumido_usd": 0.10}, f)
    ex = FakeSistemaExecutor(ativo=False)
    esp = SpyEspinha()
    st = {}
    athena_local.passada_sistema(spec, st, ex, espinha=esp, agora=1000.0, **_kw(tmp_path))

    custos = [r for r in esp.regs if r.get("tipo") == "custo"]
    # DUAS linhas de custo, medido e presumido SEPARADOS, com sistema=slug.
    medidos = [c for c in custos if c["medido"] is True]
    presumidos = [c for c in custos if c["medido"] is False]
    assert len(medidos) == 1 and medidos[0]["custo_usd"] == 0.42
    assert len(presumidos) == 1 and presumidos[0]["custo_usd"] == 0.10
    assert all(c["sistema"] == "sis1" for c in custos)
    # idempotente: um 2º ciclo com o MESMO run_id NÃO re-registra o custo.
    n = len(esp.regs)
    athena_local.passada_sistema(spec, st, ex, espinha=esp, agora=1001.0, **_kw(tmp_path))
    assert len(esp.regs) == n


def test_passada_morte_transitoria_re_tenta_sob_gate(tmp_path):
    """NEVER-STOP: uma morte TRANSITÓRIA (SIGKILL → relancar) marca retentar_devido
    e o run é RE-DISPARADO sob o gate do disjuntor. DENTE: contra o comportamento
    sem a flag, um sistema morto por recurso ficaria parado para sempre (sob_demanda
    só dispararia com pedido)."""
    spec = _spec(tmp_path)
    ex = FakeSistemaExecutor(ativo=False, obitos={
        "sis1": {"conta": "sis1", "curso": spec.raiz, "exit_code": -9,
                 "stderr_tail": "", "pid": None, "stderr_path": None}})
    esp = SpyEspinha()
    st = {}
    # ciclo do óbito: relancar → retentar_devido + re-disparo (disjuntor ainda aberto).
    athena_local.passada_sistema(spec, st, ex, espinha=esp, agora=1000.0, **_kw(tmp_path))
    assert ex.disparos == ["sis1"]                      # never-stop: re-tentou sozinho
    assert any("recozer e re-tentar" in r["o_que"] for r in esp.regs)
    assert not any("reseed" in str(r).lower() for r in esp.regs)


def test_passada_dispara_sob_pedido_sob_demanda(tmp_path):
    """Cadência sob_demanda: só dispara sob PEDIDO explícito, nunca em loop de gasto."""
    spec = _spec(tmp_path)
    ex = FakeSistemaExecutor(ativo=False)
    esp = SpyEspinha()
    # sem pedido → quieto (não gasta).
    st = {}
    acao = athena_local.passada_sistema(spec, st, ex, espinha=esp, agora=1000.0, **_kw(tmp_path))
    assert acao is None and ex.disparos == []
    # com pedido → dispara UMA vez (consome o pedido).
    st["pedido_run"] = True
    acao2 = athena_local.passada_sistema(spec, st, ex, espinha=esp, agora=1001.0, **_kw(tmp_path))
    assert ex.disparos == ["sis1"] and acao2.executada
    assert any("disparei run de sis1" in r["o_que"] for r in esp.regs)
