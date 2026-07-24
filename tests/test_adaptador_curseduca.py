"""Fiação do adaptador CURSEDUCA (white-label; tenant segueadi) no daemon doméstico.

O motor JÁ existe (/aula/motor/curseduca/, HEAD d651277) e foi PROVADO ao vivo
24/07: `python -m motor.curseduca --limit 2 --course 164` headless capturou 2/2
aulas do curso 164 (segueadi) → páginas REAIS no Notion. Estes testes cravam o
contrato da INTEGRAÇÃO (dublês, zero captura real):

  1. dispatch: `python -m motor.curseduca <url>` com a sessão EXISTENTE
     (CURSEDUCA_SESSION_PATH) e HEADLESS=1 (provado: o download HLS Bunny
     funciona headless; no motor, HEADLESS=1 => `allow_reseed=False` =>
     sessão morta ABORTA com SessionDeadError — jamais login automático);
  2. TENANT: CURSEDUCA_TENANT_UUID entra pelo spec (default segueadi, único
     tenant hoje) e é SOBREPONÍVEL por-curso via `CursoLocal.tenant` →
     `PlataformaSpec.tenant_env` (o caminho multi-tenant futuro);
  3. anti-ban 1-por-conta (dente) + perfil Chrome DEDICADO (nunca o
     `.chrome-profile` do Hotmart — ProcessSingleton);
  4. gate de domínio: membros.segueadi.com entra SEM perder os antigos;
  5. `carregar_cursos` plumba `tenant` do YAML; plataformas antigas INTACTAS
     (nenhuma ganha env de tenant — `tenant_env` só existe no curseduca).
"""
import pytest

from maestro import adaptador_pipeline, athena_local
from maestro.adaptadores import captura
from tests.test_executor_local import DIR, PY, FakeSpawn, _exec, _hot, C1

AULA = "/Users/guilhermerodrigues/teste/aula"
URL = "https://membros.segueadi.com/"
SESS = f"{AULA}/.segueadi-session.json"
TENANT_SEGUEADI = "4037b710-50c5-11ed-b97b-16058182e383"


def _curseduca(url=URL, conta="curseduca-segueadi", sess=SESS, tenant=""):
    return captura.CursoLocal(url=url, conta=conta, plataforma="curseduca",
                              session_path=sess, tenant=tenant)


# ==========================================================================
# 1) DISPATCH: módulo próprio + sessão EXISTENTE + headless fail-closed
# ==========================================================================
def test_monta_modulo_curseduca_com_sessao_existente():
    sp = FakeSpawn()
    ex = _exec(_curseduca(), spawn=sp)
    conf = ex.disparar(URL)
    assert conf                                            # confirmação truthy
    call = sp.calls[0]
    # DENTES: `python -m motor.curseduca <url>` — passe ÚNICO ("base"), sem flag
    # nenhuma (o CLI não conhece --audio/--embed) e JAMAIS --reseed (login é
    # manual, fora do daemon).
    assert call["cmd"] == [PY, "-m", "motor.curseduca", URL]
    assert "--reseed" not in call["cmd"]
    env = call["env"]
    assert env["CURSEDUCA_URL"] == URL                     # espelha o argv
    assert env["CURSEDUCA_SESSION_PATH"] == SESS           # sessão EXISTENTE
    # HEADLESS=1 (provado no smoke: HLS Bunny baixa headless) => allow_reseed=False
    # no motor: sessão morta ABORTA (SessionDeadError) e a autópsia escala reseed.
    assert env["HEADLESS"] == "1"
    assert env["WHISPER_BACKEND"] == "groq"                # INVIOLÁVEL Groq
    # channel=chrome (MOTOR_BROWSER popado): o MESMO canal que SEMEOU a sessão.
    assert "MOTOR_BROWSER" not in env
    assert call["cwd"] == DIR                              # motor vive em /aula


def test_perfil_chrome_dedicado_nunca_o_do_hotmart():
    # Sem CHROME_USER_DATA_DIR no spec => perfil POR CONTA em `_montar` — nunca
    # o `.chrome-profile` do Hotmart (ProcessSingleton mataria o 2º na largada).
    sp = FakeSpawn()
    _exec(_curseduca(), spawn=sp).disparar(URL)
    perfil = sp.calls[0]["env"]["CHROME_USER_DATA_DIR"]
    assert perfil == ".chrome-profile-curseduca-segueadi"
    assert perfil != ".chrome-profile"


# ==========================================================================
# 2) TENANT: default do spec (único tenant hoje) + override POR-CURSO (futuro)
# ==========================================================================
def test_tenant_default_do_spec_e_o_segueadi():
    # Hoje há UM tenant Curseduca (segueadi): o uuid vive no `env` do spec como
    # default — o motor exige CURSEDUCA_TENANT_UUID (resolve o vídeo no player).
    sp = FakeSpawn()
    _exec(_curseduca(), spawn=sp).disparar(URL)
    assert sp.calls[0]["env"]["CURSEDUCA_TENANT_UUID"] == TENANT_SEGUEADI


def test_tenant_por_curso_vence_o_default_do_spec():
    # MULTI-TENANT FUTURO: um 2º tenant Curseduca entra SÓ pelo YAML
    # (`tenant:` no curso), sem tocar código — `CursoLocal.tenant` →
    # `spec.tenant_env`, aplicado DEPOIS do spec.env (logo VENCE o default).
    sp = FakeSpawn()
    outro = "beef0000-0000-0000-0000-000000000000"
    _exec(_curseduca(tenant=outro), spawn=sp).disparar(URL)
    assert sp.calls[0]["env"]["CURSEDUCA_TENANT_UUID"] == outro


def test_tenant_nao_vaza_para_plataformas_sem_tenant_env():
    # ADITIVO: plataformas antigas têm tenant_env='' — mesmo que o YAML traga
    # `tenant`, NADA é injetado nelas (comportamento vivo intacto).
    sp = FakeSpawn()
    hot = captura.CursoLocal(url=C1, conta="hotmart-principal",
                             plataforma="hotmart", tenant="x-nao-vaza")
    _exec(hot, spawn=sp).disparar(C1)
    env = sp.calls[0]["env"]
    assert "CURSEDUCA_TENANT_UUID" not in env
    assert sp.calls[0]["cmd"][:3] == [PY, "-m", "motor.cli"]


def test_so_curseduca_tem_tenant_env():
    # O plumb de tenant é EXCLUSIVO do curseduca — nenhum spec antigo mudou.
    for plat, spec in captura._PLATAFORMAS.items():
        if plat == "curseduca":
            assert spec.tenant_env == "CURSEDUCA_TENANT_UUID"
        else:
            assert spec.tenant_env == ""


# ==========================================================================
# 3) DENTE ANTI-BAN: 1 motor por conta; paralelo legítimo com o Hotmart
# ==========================================================================
def test_dente_anti_ban_segunda_captura_na_mesma_conta_recusada():
    sp = FakeSpawn()
    outro = URL.rstrip("/") + "/outro-curso"
    ex = _exec(_curseduca(), _curseduca(url=outro), spawn=sp)
    ex.disparar(URL)
    with pytest.raises(captura.ContaOcupada):
        ex.disparar(outro)                                 # MESMA conta: RECUSADO
    assert len(sp.calls) == 1                              # UM único motor


def test_paralelo_com_hotmart_e_hotmart_intacto():
    # Contas DISTINTAS => paralelo legítimo; o spawn do Hotmart continua
    # EXATAMENTE como antes (perfil histórico, HEADED, sem env curseduca).
    sp = FakeSpawn()
    ex = _exec(_curseduca(), _hot(C1, "hotmart-principal"), spawn=sp)
    ex.disparar(URL)
    ex.disparar(C1)
    assert len(sp.calls) == 2
    hot_env = sp.calls[1]["env"]
    assert hot_env["CHROME_USER_DATA_DIR"] == ".chrome-profile"
    assert "HEADLESS" not in hot_env                       # Hotmart segue HEADED
    assert "CURSEDUCA_SESSION_PATH" not in hot_env
    assert "CURSEDUCA_URL" not in hot_env


# ==========================================================================
# 4) GATE DE DOMÍNIO: segueadi entra SEM perder os antigos (aditivo)
# ==========================================================================
def test_dominio_segueadi_suportado_no_default():
    assert adaptador_pipeline.plataforma_suportada(
        URL, athena_local.PLATAFORMAS_SUPORTADAS_PADRAO)


def test_sem_o_dominio_no_gate_curseduca_nao_e_despachada():
    # O dente RED da fiação: com o gate VIVO de hoje (4 hosts), segueadi é
    # barrada — é por isso que a linha nova do launch.sh é parte da integração.
    vivo_hoje = frozenset(
        {"hotmart.com", "memberkit.com.br", "stoa.com.br", "mykajabi.com"})
    assert not adaptador_pipeline.plataforma_suportada(URL, vivo_hoje)


def test_dominios_antigos_continuam_suportados():
    for u in ("https://hotmart.com/pt-br/club/x/products/1",
              "https://minha.memberkit.com.br/",
              "https://educacao.stoa.com.br/meus-cursos",
              "https://nepq-training.mykajabi.com/",
              "https://dashboard.kiwify.com.br/"):
        assert adaptador_pipeline.plataforma_suportada(
            u, athena_local.PLATAFORMAS_SUPORTADAS_PADRAO)


# ==========================================================================
# 5) YAML -> CursoLocal: `tenant` plumbado; ausência => vazio (default spec)
# ==========================================================================
def test_carregar_cursos_le_tenant(tmp_path):
    p = tmp_path / "cursos.yaml"
    p.write_text(
        "- url: https://membros.segueadi.com/\n"
        "  conta: curseduca-segueadi\n"
        "  plataforma: curseduca\n"
        f"  session_path: {SESS}\n"
        f"  tenant: {TENANT_SEGUEADI}\n"
        "- url: https://hotmart.com/pt-br/club/x/products/9\n"
        "  conta: hotmart-principal\n")
    cursos = athena_local.carregar_cursos(str(p))
    assert cursos[0].tenant == TENANT_SEGUEADI
    assert cursos[0].session_path == SESS
    assert cursos[1].tenant == ""                          # ausente => vazio
