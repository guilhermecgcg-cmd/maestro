"""Fiação dos 5 adaptadores NOVOS no daemon doméstico: kiwify, nutror, alpaclass,
hubla, greenn.

O código dos motores JÁ existe em /aula/motor/<plat>/ e as SESSÕES já foram semeadas
pelo usuário (20/07, login manual headed — NUNCA re-logar). Estes testes cravam o
contrato da INTEGRAÇÃO (dublês, zero captura real):

  1. cada plataforma aparece no dispatch (`_PLATAFORMAS`) e spawna
     `python -m motor.<plat> <url>` com o SESSION_PATH da sessão EXISTENTE;
  2. HEADLESS=1 SEMPRE => no motor, `allow_reseed=False` (fail-closed: sessão morta
     ABORTA com SessionDeadError e escala reseed — jamais login automático);
  3. anti-ban 1-por-conta: o 2º disparo na MESMA conta é RECUSADO (ContaOcupada),
     contas distintas rodam em paralelo (dente);
  4. o gate de domínio (`PLATAFORMAS_SUPORTADAS`) conhece os 5 domínios novos SEM
     perder os 4 antigos (aditivo);
  5. `carregar_cursos` plumba o `session_path` do YAML até o executor — Memberkit
     (3 tenants, sessões próprias) agora recebe MEMBERKIT_SESSION_PATH POR-CURSO
     (fix tenant-por-sessão); hotmart/stoa/kajabi (sem session_env) seguem INTACTAS.
"""
import pytest

from maestro import adaptador_pipeline, athena_local
from maestro.adaptadores import captura
from tests.test_executor_local import DIR, PY, FakeSpawn, _exec, _hot, C1

AULA = "/Users/guilhermerodrigues/teste/aula"

# (plataforma, home_url do tenant [de onde a SESSÃO real foi semeada], env da URL,
#  env do session_path, arquivo de sessão EXISTENTE)
PLATS = [
    ("kiwify", "https://dashboard.kiwify.com.br/", "KIWIFY_URL",
     "KIWIFY_SESSION_PATH", f"{AULA}/.kiwify-session.json"),
    ("nutror", "https://app.nutror.com/cursos", "NUTROR_URL",
     "NUTROR_SESSION_PATH", f"{AULA}/.nutror-session.json"),
    ("alpaclass", "https://fsp.alpaclass.com/", "ALPACLASS_URL",
     "ALPACLASS_SESSION_PATH", f"{AULA}/.alpaclass-session.json"),
    ("hubla", "https://app.hub.la", "HUBLA_URL",
     "HUBLA_SESSION_PATH", f"{AULA}/.hubla-session.json"),
    ("greenn", "https://adm.greenn.com.br/", "GREENN_URL",
     "GREENN_SESSION_PATH", f"{AULA}/.greenn-session.json"),
]


def _curso(plat, url, sess, conta=None):
    return captura.CursoLocal(url=url, conta=conta or f"{plat}-principal",
                              plataforma=plat, session_path=sess)


# ==========================================================================
# 1) DISPATCH: módulo próprio + sessão EXISTENTE + headless fail-closed
# ==========================================================================
@pytest.mark.parametrize("plat,url,url_env,sess_env,sess", PLATS)
def test_monta_modulo_proprio_com_sessao_existente(plat, url, url_env, sess_env, sess):
    sp = FakeSpawn()
    ex = _exec(_curso(plat, url, sess), spawn=sp)
    conf = ex.disparar(url)
    assert conf                                            # confirmação truthy
    call = sp.calls[0]
    # DENTES: `python -m motor.<plat> <url>` — passe ÚNICO, sem flag nenhuma e
    # JAMAIS --reseed (o único caminho de login é manual, fora do daemon).
    assert call["cmd"] == [PY, "-m", f"motor.{plat}", url]
    assert "--reseed" not in call["cmd"]
    env = call["env"]
    assert env[url_env] == url                             # espelha o argv (como Stoa/Kajabi)
    # SESSÃO EXISTENTE apontada explicitamente: o motor injeta ESTE storage_state.
    assert env[sess_env] == sess
    # HEADLESS=1 => no motor `allow_reseed=False` (fail-closed): sessão morta ABORTA
    # (SessionDeadError) e a autópsia escala reseed — NUNCA tenta logar.
    assert env["HEADLESS"] == "1"
    assert env["WHISPER_BACKEND"] == "groq"                # INVIOLÁVEL Groq
    # channel=chrome (MOTOR_BROWSER popado): o MESMO canal que SEMEOU a sessão —
    # trocar de engine mudaria o fingerprint da sessão semeada (anti-ban).
    assert "MOTOR_BROWSER" not in env
    assert call["cwd"] == DIR                              # motores vivem em /aula


@pytest.mark.parametrize("plat,url,url_env,sess_env,sess", PLATS)
def test_perfil_chrome_dedicado_por_conta(plat, url, url_env, sess_env, sess):
    # Perfil DEDICADO por conta (nunca o .chrome-profile do Hotmart): sem isto, o
    # ProcessSingleton do Chrome mata o 2º motor na largada (o flap histórico).
    sp = FakeSpawn()
    _exec(_curso(plat, url, sess), spawn=sp).disparar(url)
    perfil = sp.calls[0]["env"]["CHROME_USER_DATA_DIR"]
    assert perfil == f".chrome-profile-{plat}-principal"
    assert perfil != ".chrome-profile"                     # jamais o perfil do Hotmart


def test_sem_session_path_nao_inventa_env():
    # session_path vazio => NÃO seta o env: o motor cai no default dele
    # (.{plat}-session.json relativo ao cwd=/aula) — nunca um caminho inventado.
    sp = FakeSpawn()
    url = "https://app.hub.la"
    _exec(_curso("hubla", url, ""), spawn=sp).disparar(url)
    assert "HUBLA_SESSION_PATH" not in sp.calls[0]["env"]


# ==========================================================================
# 2) DENTE ANTI-BAN: 1 motor por conta — o 2º disparo na MESMA conta é RECUSADO
# ==========================================================================
@pytest.mark.parametrize("plat,url,url_env,sess_env,sess", PLATS)
def test_dente_anti_ban_segunda_captura_na_mesma_conta_recusada(
        plat, url, url_env, sess_env, sess):
    sp = FakeSpawn()
    outro = url.rstrip("/") + "/outro-curso"
    ex = _exec(_curso(plat, url, sess),
               _curso(plat, outro, sess),                  # MESMA conta
               spawn=sp)
    ex.disparar(url)
    with pytest.raises(captura.ContaOcupada):
        ex.disparar(outro)                                 # 2º na mesma conta: RECUSADO
    assert len(sp.calls) == 1                              # UM único motor spawnado


def test_cinco_plataformas_e_hotmart_disparam_em_paralelo():
    # Contas DISTINTAS => paralelo legítimo (o anti-ban é POR CONTA, não global).
    sp = FakeSpawn()
    cursos = [_curso(p, u, s) for (p, u, _ue, _se, s) in PLATS]
    cursos.append(_hot(C1, "hotmart-principal"))
    ex = _exec(*cursos, spawn=sp)
    for c in cursos:
        ex.disparar(c.url)
    assert len(sp.calls) == 6                              # 5 novas + hotmart
    for c in cursos:
        assert ex.conta_ocupada(c.conta)


def test_idempotente_mesmo_curso_nao_respawna():
    sp = FakeSpawn()
    (plat, url, _ue, _se, sess) = PLATS[0]
    ex = _exec(_curso(plat, url, sess), spawn=sp)
    ex.disparar(url)
    conf = ex.disparar(url)                                # mesmo curso, processo vivo
    assert conf.startswith("ja_capturando:")
    assert len(sp.calls) == 1


# ==========================================================================
# 3) GATE DE DOMÍNIO: os 5 novos entram SEM perder os 4 antigos (aditivo)
# ==========================================================================
@pytest.mark.parametrize("plat,url,url_env,sess_env,sess", PLATS)
def test_dominio_novo_e_suportado_no_default(plat, url, url_env, sess_env, sess):
    assert adaptador_pipeline.plataforma_suportada(
        url, athena_local.PLATAFORMAS_SUPORTADAS_PADRAO)


def test_dominios_antigos_continuam_suportados():
    # ADITIVO: a fiação nova não pode desligar nenhuma plataforma já viva.
    for u in ("https://hotmart.com/pt-br/club/x/products/1",
              "https://minha.memberkit.com.br/",
              "https://educacao.stoa.com.br/meus-cursos",
              "https://nepq-training.mykajabi.com/"):
        assert adaptador_pipeline.plataforma_suportada(
            u, athena_local.PLATAFORMAS_SUPORTADAS_PADRAO)


def test_sem_o_dominio_no_gate_a_plataforma_nao_e_despachada():
    # O dente RED da fiação: com o default ANTIGO (sem os 5), o gate barra — é
    # exatamente por isso que a entrada nova no default é parte da integração.
    antigo = frozenset({"hotmart.com", "memberkit.com.br", "stoa.com.br", "mykajabi.com"})
    for (_p, url, _ue, _se, _s) in PLATS:
        assert not adaptador_pipeline.plataforma_suportada(url, antigo)


# ==========================================================================
# 4) YAML -> CursoLocal: session_path plumbado; plataformas antigas INTACTAS
# ==========================================================================
def test_carregar_cursos_le_session_path(tmp_path):
    p = tmp_path / "cursos.yaml"
    p.write_text(
        "- url: https://dashboard.kiwify.com.br/\n"
        "  conta: kiwify-principal\n"
        "  plataforma: kiwify\n"
        f"  session_path: {AULA}/.kiwify-session.json\n"
        "- url: https://hotmart.com/pt-br/club/x/products/9\n"
        "  conta: hotmart-principal\n")
    cursos = athena_local.carregar_cursos(str(p))
    assert cursos[0].session_path == f"{AULA}/.kiwify-session.json"
    assert cursos[1].session_path == ""                    # ausente => vazio (default)


def test_memberkit_injeta_session_path_por_curso():
    # FIX tenant-por-sessão: o motor.memberkit lê UM único MEMBERKIT_SESSION_PATH do
    # env; com o session_env no spec, `_montar` injeta o session_path POR-CURSO (YAML)
    # nesse env — cada tenant usa a SUA sessão em vez de todos caírem no default.
    sp = FakeSpawn()
    mk = captura.CursoLocal(url="https://comunidade-triade.memberkit.com.br/",
                            conta="memberkit-triade", plataforma="memberkit",
                            session_path=f"{AULA}/.memberkit-session.json")
    _exec(mk, spawn=sp).disparar(mk.url)
    env = sp.calls[0]["env"]
    assert env["MEMBERKIT_SESSION_PATH"] == f"{AULA}/.memberkit-session.json"
    assert sp.calls[0]["cmd"] == [PY, "-m", "motor.memberkit", mk.url]


def test_memberkit_tenants_recebem_sessoes_distintas():
    # DENTE do fix: dois tenants Memberkit com session_path DIFERENTES (A e B) têm de
    # receber MEMBERKIT_SESSION_PATH distintos. SEM o plumbing (session_env=''), ambos
    # ficariam SEM o env e cairiam no MESMO default do motor => disputa da sessão.
    # Contas distintas => paralelo legítimo (anti-ban é por-conta), 2 spawns.
    sp = FakeSpawn()
    a = captura.CursoLocal(url="https://comunidade-triade.memberkit.com.br/",
                           conta="memberkit-triade", plataforma="memberkit",
                           session_path=f"{AULA}/.memberkit-session.json")
    b = captura.CursoLocal(url="https://empreenderdinheiro.memberkit.com.br/",
                           conta="memberkit-empreender", plataforma="memberkit",
                           session_path=f"{AULA}/.memberkit-empreender-session.json")
    ex = _exec(a, b, spawn=sp)
    ex.disparar(a.url)
    ex.disparar(b.url)
    envs = {c["cmd"][3]: c["env"]["MEMBERKIT_SESSION_PATH"] for c in sp.calls}
    assert envs[a.url] == f"{AULA}/.memberkit-session.json"
    assert envs[b.url] == f"{AULA}/.memberkit-empreender-session.json"
    assert envs[a.url] != envs[b.url]                      # sessões NÃO se misturam


def test_hotmart_sem_session_env_fica_intacta():
    # ADITIVO: hotmart NÃO declara session_env => mesmo trazendo session_path no YAML,
    # o executor NÃO injeta env de sessão nela (comportamento vivo intacto).
    sp = FakeSpawn()
    ht = captura.CursoLocal(url=C1, conta="hotmart-principal", plataforma="hotmart",
                            session_path=f"{AULA}/.hotmart-session.json")
    _exec(ht, spawn=sp).disparar(ht.url)
    env = sp.calls[0]["env"]
    assert "HOTMART_SESSION_PATH" not in env
    assert "MEMBERKIT_SESSION_PATH" not in env
