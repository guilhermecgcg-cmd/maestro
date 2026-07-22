"""F4-e/F4-f: DENTES dos gatilhos de correção (a Athena ACIONA o Construtor) e da
prestação de contas por sistema.

  F4-e — a Athena NUNCA edita código instalado (o hash fail-closed barra hot-patch,
    provado no lado da fábrica); a única porta é RE-RODAR o Construtor. Aqui:
    - trava REAL da cauda do ledger (corrige o rótulo fixo `irreversivel-externo`);
    - gatilho de engenharia fim-a-fim (congelado executor_ausente → disparar_build →
      build pronto → re-run FECHA o incidente);
    - disjuntor de engenharia (1 ciclo/etapa/dia; 2ª falha do incidente → humano).
  F4-f — `gasto_do_dia_por_sistema` (medido≠presumido) + gate D5 (alerta/pausa).
"""
import json
import os
import sys

from datetime import date

from maestro import athena_local, causa_sistema, decisoes, disjuntor, vigia
from maestro.adaptadores.sistema import SistemaExecutor, SistemaSpec


def _spawn_sleeper(cmd, *, env, cwd, stderr_path):
    import subprocess
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def test_disparar_build_real_exclui_run(tmp_path):
    """A porta de build usa o MESMO lock do slug: enquanto reconstrói, `build_ativo`
    é True e um `disparar` (run) é IDEMPOTENTE (ocupado) — nunca build+run juntos."""
    spec = SistemaSpec(slug="dummy", raiz=str(tmp_path / "sis"))
    ex = SistemaExecutor([spec], fabrica_python=sys.executable,
                         fabrica_dir=str(tmp_path), spawn=_spawn_sleeper,
                         lock_dir=str(tmp_path / "locks-sistemas"))
    fb = ex.escrever_feedback("dummy", {"trava": "executor_ausente", "run_id": "run1"})
    assert os.path.exists(fb)
    conf = ex.disparar_build("dummy", "e1", fb)
    assert conf.startswith("build_iniciado:dummy:etapa=e1")
    assert ex.build_ativo("dummy") is True
    assert ex.sistema_ativo("dummy") is True             # mesmo lock → ativo
    # run NÃO abre um 2º processo enquanto o build roda (1 por slug, idempotente).
    assert ex.disparar("dummy").startswith("ja_rodando:")
    # e um 2º build também é barrado (ocupado).
    assert ex.disparar_build("dummy", "e1", fb) == "ocupado:dummy"
    assert ex.matar("dummy") is True
    assert ex.build_ativo("dummy") is False


# ==========================================================================
# Fakes controláveis
# ==========================================================================
class FakeExec:
    """Espelha o contrato do SistemaExecutor com as portas de F4-e. Registra os
    disparos de run e de BUILD; `disparar_build` escreve um build-<id>.json no
    <raiz>/estado (como a fábrica faria) com `pronto` configurável."""
    def __init__(self, raiz, *, build_pronto=True):
        self.raiz = raiz
        self.build_pronto = build_pronto
        self.runs = []
        self.builds = []
        self.feedbacks = []
        self._n = 0

    def sistema_ativo(self, slug):
        return False

    def build_ativo(self, slug):
        return False

    def drenar_obitos(self):
        return {}

    def matar(self, slug, **kw):
        return True

    def disparar(self, slug):
        self.runs.append(slug)
        return f"run_iniciado:{slug}"

    def escrever_feedback(self, slug, dados):
        p = os.path.join(str(self.raiz), "estado", f"{slug}.feedback.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(dados, f)
        self.feedbacks.append(dados)
        return p

    def disparar_build(self, slug, etapa, feedback_path=None):
        self._n += 1
        bid = f"reconstrucao-{etapa}-{self._n}"
        self.builds.append((slug, etapa, feedback_path))
        d = os.path.join(str(self.raiz), "estado")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"build-{bid}.json"), "w", encoding="utf-8") as f:
            json.dump({"build_id": bid, "etapa": etapa,
                       "pronto": bool(self.build_pronto),
                       "escalar": not self.build_pronto,
                       "motivo": "ok" if self.build_pronto else "review sujo",
                       "hash_modulo": "h-novo" if self.build_pronto else "",
                       "custo_medido_usd": 0.7, "custo_presumido_usd": 0.2,
                       "smoke_laudo": ""}, f)
        return f"build_iniciado:{slug}:etapa={etapa}"


class SpyEspinha:
    def __init__(self):
        self.regs = []

    def registrar_decisao(self, o_que, por_que, **kw):
        self.regs.append({"o_que": o_que, "por_que": por_que, **kw})


def _spec(raiz, **kw):
    return SistemaSpec(slug="sis1", raiz=str(raiz), **kw)


def _escrever_resultado(raiz, run_id, *, estado, sucesso, medido=0.1, presumido=0.0):
    d = os.path.join(str(raiz), "estado")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"resultado-{run_id}.json"), "w") as f:
        json.dump({"run_id": run_id, "estado": estado, "sucesso": sucesso,
                   "custo_medido_usd": medido, "custo_presumido_usd": presumido}, f)
    # mtime distinto p/ _resultado_mais_recente ordenar corretamente
    os.utime(os.path.join(d, f"resultado-{run_id}.json"), None)


def _escrever_ledger(raiz, run_id, *, evento="g4_escalada", trava, etapa):
    d = os.path.join(str(raiz), "estado", "runs")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{run_id}.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"evento": "run_inicio"}) + "\n")
        f.write(json.dumps({"evento": evento, "trava": trava, "etapa": etapa}) + "\n")


def _kw(tmp_path):
    return dict(vigia=vigia, causa_sistema=causa_sistema, disjuntor=disjuntor,
                alertas=athena_local._AlertasNulo(),
                lock_dir=str(tmp_path / "locks-sistemas"),
                autopsia_dir=str(tmp_path / "aut-sis"), flap_min=3)


# ==========================================================================
# F4-f.1 — gasto_do_dia_por_sistema
# ==========================================================================
def test_gasto_por_sistema_agrupa_e_separa_medido_presumido(tmp_path):
    dia = date(2026, 7, 22)
    ag = None
    from datetime import datetime
    ag = datetime(2026, 7, 22, 10, 0, 0)
    base = tmp_path / "decisoes"
    # sis1: 1 medido 0.5 + 1 presumido 0.3; sis2: 1 medido 1.0; captura (None)→geral.
    decisoes.registrar_decisao("run", "x", reversivel=True, tipo="custo",
                               custo_usd=0.5, medido=True, sistema="sis1",
                               agora=ag, dir_base=base)
    decisoes.registrar_decisao("run", "x", reversivel=True, tipo="custo",
                               custo_usd=0.3, medido=False, sistema="sis1",
                               agora=ag, dir_base=base)
    decisoes.registrar_decisao("run", "x", reversivel=True, tipo="custo",
                               custo_usd=1.0, medido=True, sistema="sis2",
                               agora=ag, dir_base=base)
    decisoes.registrar_decisao("captura", "x", reversivel=True, tipo="custo",
                               custo_usd=0.9, medido=True, sistema=None,
                               agora=ag, dir_base=base)
    g = decisoes.gasto_do_dia_por_sistema(dia, base)
    assert g["sis1"]["custo_medido_usd"] == 0.5
    assert g["sis1"]["custo_presumido_usd"] == 0.3       # NUNCA somados
    assert g["sis2"]["custo_medido_usd"] == 1.0
    assert g["athena-geral"]["custo_medido_usd"] == 0.9  # sistema=None → geral
    # medido e presumido jamais aparecem num total único.
    assert "custo_usd" not in g["sis1"] and "custo_total" not in g["sis1"]


# ==========================================================================
# F4-e — trava REAL da cauda (corrige o rótulo fixo `irreversivel-externo`)
# ==========================================================================
def test_congelado_executor_ausente_vira_engenharia_nao_calibracao(tmp_path):
    """DENTE do rótulo fixo: um congelamento por `executor_ausente` (defeito de
    engenharia) DEVE ser rotulado com a trava REAL e FLAGAR engenharia. Contra o
    código antigo (literal `irreversivel-externo` fixo) a correção NUNCA
    dispararia — o defeito viraria 'aguardando calibração'."""
    spec = _spec(tmp_path)
    _escrever_resultado(tmp_path, "run1", estado="congelado_parcial", sucesso=False)
    _escrever_ledger(tmp_path, "run1", trava="executor_ausente", etapa="e1")
    ex = FakeExec(tmp_path)
    esp = SpyEspinha()
    st = {}
    athena_local.passada_sistema(spec, st, ex, espinha=esp, agora=1000.0, **_kw(tmp_path))
    # trava REAL no desfecho (não o literal fixo).
    desfecho = [r for r in esp.regs if "→ congelado_parcial" in r["o_que"]]
    assert desfecho and desfecho[0]["trava"] == "executor_ausente"
    # engenharia foi ACIONADA (build disparado p/ a etapa e1).
    assert ex.builds and ex.builds[0][1] == "e1"
    assert any("aciono engenharia p/ sis1:e1" in r["o_que"] for r in esp.regs)


def test_congelado_irreversivel_externo_nao_reescreve(tmp_path):
    """Congelamento D2 (calibração pendente) → trava `irreversivel-externo`,
    NENHUMA reescrita (não é defeito). O flip do flag é do usuário."""
    spec = _spec(tmp_path)
    _escrever_resultado(tmp_path, "run1", estado="congelado_parcial", sucesso=False)
    _escrever_ledger(tmp_path, "run1", trava="irreversivel-externo", etapa="e2")
    ex = FakeExec(tmp_path)
    esp = SpyEspinha()
    st = {}
    athena_local.passada_sistema(spec, st, ex, espinha=esp, agora=1000.0, **_kw(tmp_path))
    assert ex.builds == []                               # NÃO reescreveu
    desfecho = [r for r in esp.regs if "→ congelado_parcial" in r["o_que"]]
    assert desfecho[0]["trava"] == "irreversivel-externo"
    assert "eng_pendente" not in st


# ==========================================================================
# F4-e — gatilho de engenharia FIM-A-FIM (build pronto → re-run FECHA incidente)
# ==========================================================================
def test_engenharia_fim_a_fim_fecha_incidente(tmp_path):
    spec = _spec(tmp_path)
    ex = FakeExec(tmp_path, build_pronto=True)
    esp = SpyEspinha()
    st = {}
    k = _kw(tmp_path)

    # Ciclo 1: run congelado por executor_ausente → dispara reconstrução da etapa.
    _escrever_resultado(tmp_path, "run1", estado="congelado_parcial", sucesso=False)
    _escrever_ledger(tmp_path, "run1", trava="executor_ausente", etapa="e1")
    a1 = athena_local.passada_sistema(spec, st, ex, espinha=esp, agora=1000.0, **k)
    assert ex.builds and ex.builds[0][1] == "e1"
    assert ex.feedbacks and ex.feedbacks[0]["trava"] == "executor_ausente"
    assert st["eng"]["e1"]["aguardando_build"] is True

    # Ciclo 2: build pronto observado → custo da reescrita + re-run disparado.
    a2 = athena_local.passada_sistema(spec, st, ex, espinha=esp, agora=1100.0, **k)
    custos_reescrita = [r for r in esp.regs if r.get("origem") == "athena/reescrita"
                        and r.get("tipo") == "custo"]
    medidos = [c for c in custos_reescrita if c["medido"] is True]
    presumidos = [c for c in custos_reescrita if c["medido"] is False]
    assert len(medidos) == 1 and medidos[0]["custo_usd"] == 0.7
    assert len(presumidos) == 1 and presumidos[0]["custo_usd"] == 0.2
    assert any("PRONTA (hash novo)" in r["o_que"] for r in esp.regs)
    assert ex.runs == ["sis1"]                           # re-run disparado sob gate
    assert st["eng"]["e1"]["aguardando_rerun"] is True

    # Ciclo 3: re-run APROVOU → FECHA o incidente.
    _escrever_resultado(tmp_path, "run2", estado="aprovado", sucesso=True)
    athena_local.passada_sistema(spec, st, ex, espinha=esp, agora=1200.0, **k)
    assert any("fechou incidente run1" in r["o_que"] for r in esp.regs)
    assert st["eng"]["e1"]["aguardando_rerun"] is False


# ==========================================================================
# F4-e — disjuntor de engenharia
# ==========================================================================
def test_disjuntor_engenharia_2o_gatilho_no_dia_nao_constroi(tmp_path):
    """2º gatilho de engenharia na MESMA etapa no MESMO dia → NENHUM build; espinha
    registra a escalada. (1 ciclo/etapa/dia local.)"""
    spec = _spec(tmp_path)
    ex = FakeExec(tmp_path)
    esp = SpyEspinha()
    dia = athena_local._dia_local_ts(1000.0)
    st = {"eng": {"e1": {"dia": dia, "incidente": "run0", "builds": 1,
                         "aguardando_build": False, "aguardando_rerun": False}},
          "eng_pendente": {"etapa": "e1", "incidente": "runX", "classe": "executor_ausente"}}
    acao = athena_local._passo_engenharia(
        "sis1", spec, st, ex, alertas=athena_local._AlertasNulo(), espinha=esp,
        agora=1000.0, gasto_por_sistema_fn=None, budget_modo="alerta", llm=None)
    assert ex.builds == []                               # 2º build no dia BARRADO
    assert any("2º gatilho de engenharia em e1 hoje" in r["o_que"] for r in esp.regs)


def test_2a_falha_do_mesmo_incidente_escala_humano(tmp_path):
    """Build pronto → re-run falhou de novo (mesma etapa, engenharia) = 2ª falha do
    incidente → escalar_humano, NUNCA um 3º build."""
    spec = _spec(tmp_path)
    ex = FakeExec(tmp_path)
    esp = SpyEspinha()
    # incidente aberto aguardando re-run (do build do incidente run1).
    st = {"eng": {"e1": {"dia": "2026-07-22", "incidente": "run1", "builds": 1,
                         "aguardando_build": False, "aguardando_rerun": True}}}
    _escrever_resultado(tmp_path, "run2", estado="congelado_parcial", sucesso=False)
    _escrever_ledger(tmp_path, "run2", trava="executor_ausente", etapa="e1")
    athena_local.passada_sistema(spec, st, ex, espinha=esp, agora=1000.0, **_kw(tmp_path))
    assert ex.builds == []                               # nunca um 3º build
    assert any("só humano destrava" in r["o_que"] for r in esp.regs)
    assert st["eng"]["e1"]["aguardando_rerun"] is False
    assert "eng_pendente" not in st


# ==========================================================================
# F4-f.2 — gate D5 (alerta avisa e dispara; pausa bloqueia)
# ==========================================================================
def test_d5_alerta_avisa_e_dispara(tmp_path):
    """Modo alerta (default): custo do dia ≥ teto → registra+alerta E DISPARA
    (não pausa). Latch 1×/dia/sistema."""
    spec = _spec(tmp_path, teto_dia_usd=1.0)
    ex = FakeExec(tmp_path)
    esp = SpyEspinha()
    gasto = lambda: {"sis1": {"custo_medido_usd": 0.9, "custo_presumido_usd": 0.5}}
    st = {"pedido_run": True}
    acao = athena_local.passada_sistema(
        spec, st, ex, espinha=esp, agora=1000.0,
        gasto_por_sistema_fn=gasto, budget_modo="alerta", **_kw(tmp_path))
    assert ex.runs == ["sis1"]                           # DISPAROU mesmo acima do teto
    d5 = [r for r in esp.regs if r.get("trava") == "budget"]
    assert d5 and "teto D5 atingido" in d5[0]["o_que"]
    # latch: 2º ciclo acima do teto NÃO re-registra o alerta D5.
    n_budget = len(d5)
    st["pedido_run"] = True
    athena_local.passada_sistema(
        spec, st, ex, espinha=esp, agora=1000.0,
        gasto_por_sistema_fn=gasto, budget_modo="alerta", **_kw(tmp_path))
    assert len([r for r in esp.regs if r.get("trava") == "budget"]) == n_budget


def test_d5_pausa_bloqueia(tmp_path):
    """Modo pausa (flip do usuário): custo ≥ teto → NÃO dispara (nem run nem build)."""
    spec = _spec(tmp_path, teto_dia_usd=1.0)
    ex = FakeExec(tmp_path)
    esp = SpyEspinha()
    gasto = lambda: {"sis1": {"custo_medido_usd": 1.2, "custo_presumido_usd": 0.0}}
    st = {"pedido_run": True}
    acao = athena_local.passada_sistema(
        spec, st, ex, espinha=esp, agora=1000.0,
        gasto_por_sistema_fn=gasto, budget_modo="pausa", **_kw(tmp_path))
    assert ex.runs == []                                 # PAUSA: não disparou
    assert acao is None
    assert st.get("pedido_run") is True                  # pedido explícito PRESERVADO


def test_d5_pausa_mantem_incidente_pendente(tmp_path):
    """Never-stop: em modo pausa o gatilho de engenharia NÃO é descartado — fica
    pendente e reconstrói quando o teto reabrir (aqui: gasto cai abaixo do teto)."""
    spec = _spec(tmp_path, teto_dia_usd=1.0)
    ex = FakeExec(tmp_path)
    esp = SpyEspinha()
    acima = {"sis1": {"custo_medido_usd": 1.5, "custo_presumido_usd": 0.0}}
    st = {"eng_pendente": {"etapa": "e1", "incidente": "run1", "classe": "executor_ausente"}}
    athena_local._passo_engenharia(
        "sis1", spec, st, ex, alertas=athena_local._AlertasNulo(), espinha=esp,
        agora=1000.0, gasto_por_sistema_fn=lambda: acima, budget_modo="pausa", llm=None)
    assert ex.builds == [] and st.get("eng_pendente")    # pendente preservado
    abaixo = {"sis1": {"custo_medido_usd": 0.1, "custo_presumido_usd": 0.0}}
    athena_local._passo_engenharia(
        "sis1", spec, st, ex, alertas=athena_local._AlertasNulo(), espinha=esp,
        agora=1000.0, gasto_por_sistema_fn=lambda: abaixo, budget_modo="pausa", llm=None)
    assert ex.builds and ex.builds[0][1] == "e1" and "eng_pendente" not in st


def test_d5_abaixo_do_teto_dispara_sem_alerta(tmp_path):
    spec = _spec(tmp_path, teto_dia_usd=5.0)
    ex = FakeExec(tmp_path)
    esp = SpyEspinha()
    gasto = lambda: {"sis1": {"custo_medido_usd": 0.2, "custo_presumido_usd": 0.1}}
    st = {"pedido_run": True}
    athena_local.passada_sistema(
        spec, st, ex, espinha=esp, agora=1000.0,
        gasto_por_sistema_fn=gasto, budget_modo="alerta", **_kw(tmp_path))
    assert ex.runs == ["sis1"]
    assert not any(r.get("trava") == "budget" for r in esp.regs)


# ==========================================================================
# F4-e — build em andamento: passada fica quieta (nunca mata/dispara)
# ==========================================================================
def test_build_ativo_passada_quieta(tmp_path):
    spec = _spec(tmp_path)

    class ExecBuildAtivo(FakeExec):
        def build_ativo(self, slug):
            return True

    ex = ExecBuildAtivo(tmp_path)
    esp = SpyEspinha()
    st = {"pedido_run": True}
    acao = athena_local.passada_sistema(spec, st, ex, espinha=esp, agora=1000.0,
                                        **_kw(tmp_path))
    assert acao is None and ex.runs == []                # não dispara durante um build
