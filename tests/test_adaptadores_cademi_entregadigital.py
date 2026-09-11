"""Registro das plataformas CADEMÍ e ENTREGA DIGITAL no daemon (rodada 9) — REGISTRADAS e
DESLIGADAS: nada dispara sem entrada no YAML e sem o host no gate de domínio.

Dublês com dentes (zero captura, zero sessão real, nenhum arquivo vivo):
  1. spec montado com os envs que os CLIs dos motores leem (CADEMI_URL/CADEMI_SESSION_PATH,
     ENTREGADIGITAL_URL/ENTREGADIGITAL_SESSION_PATH), HEADLESS, passe único, channel=chrome;
  2. perfil Chrome POR CONTA (`_perfil_de_conta`), nunca o `.chrome-profile` do Hotmart —
     nem quando o ambiente/extra_env do daemon vaza esse perfil;
  3. gate: os 3 hosts Cademí (exatos, sem sufixo comum) e `*.entregadigital.app.br`
     passam; host fora do gate NÃO dispara (ciclo real com o LocalExecutor real);
  4. host -> plataforma: host Cademí/Entrega Digital com outra `plataforma` no YAML é
     recusado ANTES de qualquer spawn — um dente POR HOST da trava; e o default do YAML é
     FAIL-CLOSED: sem `plataforma:`, "hotmart" só num host hotmart.com (um 5º tenant de
     domínio próprio posto no gate sem a linha é recusado NOMEADO, nunca o motor Hotmart
     no `.chrome-profile`); `plataforma: hotmart` fora do hotmart.com também;
  5. credencial por tenant: sem `session_path` => recusa; o mesmo arquivo em duas
     contas/dois hosts => recusa;
  6. tracker: pendências/terminais lidos por course_id `cademi:<host>:<id>` e
     `entregadigital:<tenant>:product:<pid>` (tenant inteiro, sem vazar para outro tenant
     nem para o `/products/<id>` do Hotmart); o reaper não toca essas plataformas;
  7. progresso no Notion: a Origem da Entrega Digital (`<tenant>.appmagic.link`) não mora
     sob a URL do YAML — o daemon conta pela Origem real, senão o progresso seria 0.
"""
import os
import sqlite3
import time

import pytest

from maestro import adaptador_pipeline, athena_local
from maestro.adaptadores import captura
from maestro.causa import _RE_SESSAO, _RE_TOKEN
from tests.test_executor_local import DIR, PY, FakeSpawn, _exec, _hot, C1

ALFA = "https://membros.alfaresearch.com.br/"
VIRAL = "https://cursos.codigoviral.com.br/"
RAMON = "https://aulas.ramonpereira.com.br/"
LUANA = "https://luanacarolina.entregadigital.app.br/"

# (plataforma, url do tenant, conta, env da URL, env da sessão, session_path)
NOVAS = [
    ("cademi", ALFA, "cademi-alfaresearch", "CADEMI_URL", "CADEMI_SESSION_PATH",
     f"{DIR}/.cademi-session-membros.alfaresearch.com.br.json"),
    ("cademi", VIRAL, "cademi-codigoviral", "CADEMI_URL", "CADEMI_SESSION_PATH",
     f"{DIR}/.cademi-session-cursos.codigoviral.com.br.json"),
    ("cademi", RAMON, "cademi-ramonpereira", "CADEMI_URL", "CADEMI_SESSION_PATH",
     f"{DIR}/.cademi-session-aulas.ramonpereira.com.br.json"),
    ("entregadigital", LUANA, "entregadigital-luanacarolina", "ENTREGADIGITAL_URL",
     "ENTREGADIGITAL_SESSION_PATH", f"{DIR}/.entregadigital-luanacarolina-session.json"),
]


def _c(plat, url, conta, sess=""):
    return captura.CursoLocal(url=url, conta=conta, plataforma=plat, session_path=sess)


def _sem_ancora_de_morte(texto):
    return not _RE_SESSAO.search(texto) and not _RE_TOKEN.search(texto)


# ==========================================================================
# 1) SPEC: módulo, envs dos CLIs, headless, passe único, channel=chrome
# ==========================================================================
@pytest.mark.parametrize("plat,url,conta,url_env,sess_env,sess", NOVAS)
def test_dispara_o_modulo_proprio_com_os_envs_do_cli(plat, url, conta, url_env, sess_env,
                                                     sess):
    sp = FakeSpawn()
    conf = _exec(_c(plat, url, conta, sess), spawn=sp).disparar(url)
    assert conf.startswith("local_iniciada:")
    call = sp.calls[0]
    # passe ÚNICO: sem flag nenhuma e JAMAIS --reseed (login é só manual, fora do daemon)
    assert call["cmd"] == [PY, "-m", f"motor.{plat}", url]
    env = call["env"]
    assert env[url_env] == url
    assert env[sess_env] == sess                           # a credencial DESTE tenant
    assert env["HEADLESS"] == "1"                          # no motor: allow_reseed=False
    assert "MOTOR_BROWSER" not in env                      # channel=chrome
    assert env["WHISPER_BACKEND"] == "groq"
    assert env["PYTHONPATH"] == DIR and call["cwd"] == DIR


@pytest.mark.parametrize("plat,url_env,sess_env,hosts", [
    ("cademi", "CADEMI_URL", "CADEMI_SESSION_PATH",
     {"membros.alfaresearch.com.br", "cursos.codigoviral.com.br",
      "aulas.ramonpereira.com.br"}),
    ("entregadigital", "ENTREGADIGITAL_URL", "ENTREGADIGITAL_SESSION_PATH",
     {"entregadigital.app.br"}),
])
def test_spec_registrado_exatamente(plat, url_env, sess_env, hosts):
    spec = captura._PLATAFORMAS[plat]
    assert spec.modulo == f"motor.{plat}"
    assert spec.passes == ("base",)
    assert spec.headless is True and spec.chromium is False
    assert (spec.url_env, spec.session_env) == (url_env, sess_env)
    assert spec.sessao_por_tenant is True
    # a trava host -> plataforma, host a host (tirar um deles falha AQUI e no teste da
    # seção 4 que dispara cada host com a plataforma errada)
    assert len(spec.hosts) == len(hosts) and set(spec.hosts) == hosts
    # nenhum perfil FIXO no spec => `_montar` dá o perfil da CONTA (`_perfil_de_conta`)
    assert "CHROME_USER_DATA_DIR" not in dict(spec.env)


# Contrato com o CÓDIGO do motor: o nome do env que o spec injeta é o que o CLI LÊ. Lê o
# fonte (texto, sem importar) da primeira árvore do motor que tiver o pacote; sem nenhuma
# árvore na máquina, pula (a checagem acima segue valendo).
_ARVORES_MOTOR = [p for p in (
    os.environ.get("ATHENA_MOTOR_DIR"),
    "/Users/guilhermerodrigues/teste/_wf2/motor-cademi",
    "/Users/guilhermerodrigues/teste/_wf2/motor-edm",
    "/Users/guilhermerodrigues/teste/aula") if p]


@pytest.mark.parametrize("plat", ["cademi", "entregadigital"])
def test_envs_do_spec_sao_os_que_o_cli_do_motor_le(plat):
    fonte = None
    for raiz in _ARVORES_MOTOR:
        cli = os.path.join(raiz, "motor", plat, "cli.py")
        if os.path.isfile(cli):
            with open(cli, encoding="utf-8") as f:
                fonte = f.read()
            break
    if fonte is None:
        pytest.skip(f"nenhuma árvore do motor com motor/{plat}/cli.py nesta máquina")
    spec = captura._PLATAFORMAS[plat]
    assert f'os.getenv("{spec.url_env}")' in fonte
    assert f'os.getenv("{spec.session_env}"' in fonte


def _fonte_do_cli(plat):
    for raiz in _ARVORES_MOTOR:
        cli = os.path.join(raiz, "motor", plat, "cli.py")
        if os.path.isfile(cli):
            with open(cli, encoding="utf-8") as f:
                return f.read()
    return None


def test_cli_da_entregadigital_r9_e_por_tenant_e_recusa_o_perfil_do_hotmart():
    # a doc do spec/`_checar_host_e_credencial` diz: o CLI da rodada 9 deriva perfil e
    # sessão POR TENANT (os mesmos nomes do daemon) e RECUSA o `.chrome-profile`; o
    # anterior (902105c) caía nele e num arquivo único. Conferido no FONTE do motor.
    fonte = _fonte_do_cli("entregadigital")
    if fonte is None:
        pytest.skip("nenhuma árvore do motor com motor/entregadigital/cli.py")
    assert 'return f".chrome-profile-entregadigital-{_slug(tenant_of(home_url))}"' in fonte
    assert 'return f".entregadigital-{_slug(tenant_of(home_url))}-session.json"' in fonte
    assert 'PERFIL_HOTMART = ".chrome-profile"' in fonte
    assert "if Path(perfil).name == PERFIL_HOTMART:" in fonte
    assert 'DEFAULT_SESSION_PATH = ".entregadigital-session.json"' not in fonte
    # o perfil que o daemon dá à conta do YAML é o MESMO nome que o CLI deriva
    assert captura._perfil_de_conta("entregadigital-luanacarolina") == \
        ".chrome-profile-entregadigital-luanacarolina"


# ==========================================================================
# 2) PERFIL POR CONTA — nunca o `.chrome-profile` do Hotmart
# ==========================================================================
@pytest.mark.parametrize("plat,url,conta,url_env,sess_env,sess", NOVAS)
def test_perfil_da_conta_mesmo_com_o_perfil_do_hotmart_vazado(monkeypatch, plat, url, conta,
                                                             url_env, sess_env, sess):
    # um CHROME_USER_DATA_DIR herdado do ambiente/extra_env do daemon NÃO pode vencer o
    # perfil da conta. (Os CLIs da rodada 9 já recusam o `.chrome-profile`; o daemon
    # forçar o da conta cobre também uma árvore com o CLI anterior da Entrega Digital,
    # 902105c, que caía nele sem a env.)
    monkeypatch.setenv("CHROME_USER_DATA_DIR", ".chrome-profile")
    sp = FakeSpawn()
    ex = _exec(_c(plat, url, conta, sess), spawn=sp,
               extra_env={"CHROME_USER_DATA_DIR": ".chrome-profile"})
    ex.disparar(url)
    perfil = sp.calls[0]["env"]["CHROME_USER_DATA_DIR"]
    assert perfil == captura._perfil_de_conta(conta) == f".chrome-profile-{conta}"
    assert perfil != ".chrome-profile"


def test_tres_tenants_cademi_tres_perfis_e_paralelo_entre_contas():
    sp = FakeSpawn()
    cursos = [_c(p, u, c, s) for (p, u, c, _ue, _se, s) in NOVAS]
    cursos.append(_hot(C1, "hotmart-principal"))
    ex = _exec(*cursos, spawn=sp)
    for c in cursos:
        ex.disparar(c.url)
    perfis = [call["env"]["CHROME_USER_DATA_DIR"] for call in sp.calls]
    assert len(sp.calls) == 5 and len(set(perfis)) == 5    # um perfil por conta
    assert perfis[-1] == ".chrome-profile"                 # só o Hotmart fica no dele


def test_mesma_conta_cademi_nunca_dois_motores():
    sp = FakeSpawn()
    (plat, url, conta, _ue, _se, sess) = NOVAS[0]
    outro = url + "area/vitrine/home"
    ex = _exec(_c(plat, url, conta, sess), _c(plat, outro, conta, sess), spawn=sp)
    ex.disparar(url)
    with pytest.raises(captura.ContaOcupada):
        ex.disparar(outro)
    assert len(sp.calls) == 1


# ==========================================================================
# 3) GATE DE DOMÍNIO — hosts exatos (Cademí) e sufixo (Entrega Digital)
# ==========================================================================
@pytest.mark.parametrize("url", [ALFA, VIRAL, RAMON, LUANA,
                                 "https://outratenant.entregadigital.app.br/"])
def test_hosts_novos_passam_o_gate_default(url):
    assert adaptador_pipeline.plataforma_suportada(
        url, athena_local.PLATAFORMAS_SUPORTADAS_PADRAO)


@pytest.mark.parametrize("url", [
    "https://evilmembros.alfaresearch.com.br/",        # prefixo parecido
    "https://membros.alfaresearch.com.br.evil.com/",   # host alheio com o nosso de prefixo
    "https://alfaresearch.com.br/",                    # o domínio pai não é o tenant
    "https://core.cademi.com.br/",                     # alvo do CNAME, não é tenant
    "https://aulas.outrotenant.com.br/",               # tenant Cademí não cadastrado
    "https://fakeentregadigital.app.br/",
    "https://entregadigital.app.br.evil.com/",
])
def test_host_parecido_ou_nao_cadastrado_nao_passa_o_gate(url):
    assert not adaptador_pipeline.plataforma_suportada(
        url, athena_local.PLATAFORMAS_SUPORTADAS_PADRAO)


def test_gate_default_segue_com_todos_os_antigos():
    antigos = ("hotmart.com", "memberkit.com.br", "stoa.com.br", "mykajabi.com",
               "kiwify.com.br", "nutror.com", "alpaclass.com", "hub.la", "greenn.club",
               "membros.segueadi.com")
    assert athena_local.PLATAFORMAS_SUPORTADAS_PADRAO[:len(antigos)] == antigos


class _VozMin:
    def __init__(self):
        self.escaladas = []

    def avisar_acao(self, acao):
        pass

    def escalar(self, problema, pedido):
        self.escaladas.append(problema.tipo)


def _ciclo(cursos, ex, plataformas):
    voz = _VozMin()
    athena_local.ciclo_local(cursos, ex, lambda curso: (0, 0), voz, {}, {}, agora=1000.0,
                             plataformas_suportadas=plataformas)
    return voz


def test_host_fora_do_gate_nao_dispara_no_ciclo_real():
    # tenant Cademí NÃO cadastrado no gate, com YAML completo: o ciclo PULA (plataforma
    # nova) e o LocalExecutor REAL não spawna nada.
    fora = "https://aulas.outrotenant.com.br/"
    cursos = [_c("cademi", fora, "cademi-outro", f"{DIR}/.cademi-outro.json")]
    sp = FakeSpawn()
    voz = _ciclo(cursos, _exec(*cursos, spawn=sp),
                 frozenset(athena_local.PLATAFORMAS_SUPORTADAS_PADRAO))
    assert sp.calls == []
    assert "plataforma_nova" in voz.escaladas


def test_gate_vivo_sem_os_hosts_novos_mantem_desligado_e_o_default_liga():
    # o launch.sh vivo fixa PLATAFORMAS_SUPORTADAS SEM os 4 hosts novos: mesmo com a
    # entrada no YAML, nada dispara (desligado). Controle positivo: com o gate que os
    # contém, o MESMO curso spawna o motor.cademi (o teste acima não passa por acaso).
    vivo_hoje = frozenset({"hotmart.com", "memberkit.com.br", "stoa.com.br", "mykajabi.com",
                           "kiwify.com.br", "nutror.com", "alpaclass.com", "hub.la",
                           "greenn.club", "membros.segueadi.com"})
    (plat, url, conta, _ue, _se, sess) = NOVAS[0]
    cursos = [_c(plat, url, conta, sess)]
    sp = FakeSpawn()
    _ciclo(cursos, _exec(*cursos, spawn=sp), vivo_hoje)
    assert sp.calls == []
    sp2 = FakeSpawn()
    _ciclo(cursos, _exec(*cursos, spawn=sp2),
           frozenset(athena_local.PLATAFORMAS_SUPORTADAS_PADRAO))
    assert [c["cmd"][2] for c in sp2.calls] == ["motor.cademi"]


# ==========================================================================
# 4) HOST -> PLATAFORMA: a linha `plataforma:` esquecida não roda o motor Hotmart
# ==========================================================================
NOVO_TENANT = "https://aulas.novotenant.com.br/"          # 5º Cademí, domínio próprio


def _yaml_sem_plataforma(tmp_path, url, conta, sess):
    p = tmp_path / "cursos.yaml"
    p.write_text(f"- url: \"{url}\"\n  conta: {conta}\n  session_path: {sess}\n")
    return athena_local.carregar_cursos(str(p))


@pytest.mark.parametrize("url,conta,dono", [
    (ALFA, "cademi-alfaresearch", "cademi"),
    (VIRAL, "cademi-codigoviral", "cademi"),
    (RAMON, "cademi-ramonpereira", "cademi"),
    (LUANA, "entregadigital-luanacarolina", "entregadigital"),
    (NOVO_TENANT, "cademi-novotenant", None),              # fora da trava `hosts`
])
def test_yaml_sem_plataforma_fora_do_hotmart_e_recusado_nomeado(tmp_path, monkeypatch,
                                                               url, conta, dono):
    limpos = []
    monkeypatch.setattr(captura, "_limpar_singleton_orfao", limpos.append)
    cursos = _yaml_sem_plataforma(tmp_path, url, conta, f"{DIR}/.{conta}-session.json")
    # o default do carregador NUNCA é "hotmart" fora do hotmart.com
    assert cursos[0].plataforma == captura.PLATAFORMA_NAO_DECLARADA
    sp = FakeSpawn()
    ex = _exec(*cursos, spawn=sp, lock_dir=str(tmp_path / "locks"))
    with pytest.raises(RuntimeError) as e:
        ex.disparar(url)
    msg = str(e.value)
    assert sp.calls == []                                  # nenhum motor (nem o Hotmart)
    assert limpos == []                                    # nenhum Singleton* apagado
    assert not ex.conta_ocupada(conta)                     # nem lock
    assert msg.startswith("sem plataforma: no YAML e host fora do hotmart.com")
    assert "não disparo" in msg[:160]                      # o alerta da passada corta em 160
    if dono:                                               # a dica aponta a plataforma certa
        assert f"declare plataforma: {dono}" in msg[:160]
    assert _sem_ancora_de_morte(msg)


def test_yaml_sem_plataforma_no_hotmart_segue_hotmart(tmp_path, monkeypatch):
    # controle positivo: o default para o Hotmart continua o de sempre (o teste acima não
    # passa por recusar tudo).
    limpos = []
    monkeypatch.setattr(captura, "_limpar_singleton_orfao", limpos.append)
    cursos = _yaml_sem_plataforma(tmp_path, C1, "hotmart-principal", "")
    assert cursos[0].plataforma == "hotmart"
    sp = FakeSpawn()
    _exec(*cursos, spawn=sp).disparar(C1)
    assert sp.calls[0]["cmd"][:3] == [PY, "-m", "motor.cli"]
    assert sp.calls[0]["env"]["CHROME_USER_DATA_DIR"] == ".chrome-profile"
    assert limpos == [os.path.join(DIR, ".chrome-profile")]


@pytest.mark.parametrize("url,esperada", [
    ("https://hotmart.com/pt-br/club/x/products/1", "hotmart"),
    ("https://sub.hotmart.com/x/products/1", "hotmart"),
    ("https://www.hotmart.com/x/products/1", "hotmart"),
    ("https://HOTMART.COM/x/products/1", "hotmart"),
    ("https://evilhotmart.com/x/products/1", captura.PLATAFORMA_NAO_DECLARADA),
    ("https://hotmart.com.evil.com/x/products/1", captura.PLATAFORMA_NAO_DECLARADA),
    ("https://hotmart.com.br/x/products/1", captura.PLATAFORMA_NAO_DECLARADA),
    (ALFA, captura.PLATAFORMA_NAO_DECLARADA),
    (NOVO_TENANT, captura.PLATAFORMA_NAO_DECLARADA),
    ("https://minha.memberkit.com.br/9", captura.PLATAFORMA_NAO_DECLARADA),
    ("sem-host", captura.PLATAFORMA_NAO_DECLARADA),
])
def test_plataforma_padrao_so_e_hotmart_no_host_do_hotmart(url, esperada):
    assert captura.plataforma_padrao(url) == esperada


def test_sentinela_nunca_tem_spec():
    assert captura.PLATAFORMA_NAO_DECLARADA not in captura._PLATAFORMAS


@pytest.mark.parametrize("valor", ["", "  ", "null"])
def test_yaml_com_plataforma_vazia_segue_a_regra_do_host(tmp_path, valor):
    p = tmp_path / "cursos.yaml"
    linha = "plataforma:" if valor == "null" else f"plataforma: \"{valor}\""
    p.write_text(f"- url: \"{NOVO_TENANT}\"\n  conta: x\n  {linha}\n"
                 f"- url: \"{C1}\"\n  conta: h\n  {linha}\n")
    cursos = athena_local.carregar_cursos(str(p))
    assert [c.plataforma for c in cursos] == [captura.PLATAFORMA_NAO_DECLARADA, "hotmart"]


def test_quinto_tenant_no_gate_sem_plataforma_nao_dispara_no_ciclo_real(tmp_path,
                                                                        monkeypatch):
    # o cenário do achado, ponta a ponta no ciclo REAL: o host novo ESTÁ no gate, a entrada
    # não tem `plataforma:`. Antes: motor.cli, HEADED, `.chrome-profile`, Singleton* do
    # perfil do Hotmart apagados. Agora: nada spawna e a escalada é NOMEADA.
    limpos = []
    monkeypatch.setattr(captura, "_limpar_singleton_orfao", limpos.append)
    cursos = _yaml_sem_plataforma(tmp_path, NOVO_TENANT, "cademi-novotenant",
                                  f"{DIR}/.cademi-session-aulas.novotenant.com.br.json")
    sp = FakeSpawn()
    pedidos = []

    class _Voz(_VozMin):
        def escalar(self, problema, pedido):
            super().escalar(problema, pedido)
            pedidos.append(pedido)

    gate = frozenset(athena_local.PLATAFORMAS_SUPORTADAS_PADRAO) | {"aulas.novotenant.com.br"}
    voz = _Voz()
    athena_local.ciclo_local(cursos, _exec(*cursos, spawn=sp, lock_dir=str(tmp_path / "l")),
                             lambda curso: (0, 0), voz, {}, {}, agora=1000.0,
                             plataformas_suportadas=gate)
    assert sp.calls == [] and limpos == []
    assert voz.escaladas == ["captura_local_disparo_falhou"]
    assert "sem plataforma: no YAML" in pedidos[0] and _sem_ancora_de_morte(pedidos[0])


@pytest.mark.parametrize("url", [NOVO_TENANT, "https://evilhotmart.com/x/products/1",
                                 "https://minha.memberkit.com.br/9"])
def test_plataforma_hotmart_explicita_fora_do_hotmart_e_recusada(tmp_path, monkeypatch, url):
    limpos = []
    monkeypatch.setattr(captura, "_limpar_singleton_orfao", limpos.append)
    sp = FakeSpawn()
    ex = _exec(_hot(url, "conta-x"), spawn=sp, lock_dir=str(tmp_path / "locks"))
    with pytest.raises(RuntimeError) as e:
        ex.disparar(url)
    assert sp.calls == [] and limpos == [] and not ex.conta_ocupada("conta-x")
    assert str(e.value).startswith("plataforma hotmart num host fora do hotmart.com")
    assert _sem_ancora_de_morte(str(e.value))


# nome de conta HOSTIL (o YAML é texto livre): nenhuma recusa do daemon casa as âncoras de
# morte do classificador — a conta passa pelo `rotulo_seguro` como a URL.
_CONTA_HOSTIL = "sessão morta unauthorized re-login"


@pytest.mark.parametrize("curso", [
    captura.CursoLocal(NOVO_TENANT, _CONTA_HOSTIL, captura.PLATAFORMA_NAO_DECLARADA),
    captura.CursoLocal(NOVO_TENANT, _CONTA_HOSTIL, "hotmart"),
    captura.CursoLocal(RAMON, _CONTA_HOSTIL, "kiwify", session_path=f"{DIR}/.k.json"),
    captura.CursoLocal(RAMON, _CONTA_HOSTIL, "cademi"),
], ids=["nao-declarada", "hotmart-fora", "trava-host", "sem-session"])
def test_recusas_nunca_casam_ancora_de_morte_nem_com_conta_hostil(curso):
    sp = FakeSpawn()
    with pytest.raises(RuntimeError) as e:
        _exec(curso, spawn=sp).disparar(curso.url)
    assert sp.calls == [] and _sem_ancora_de_morte(str(e.value))


def test_sessao_partilhada_com_conta_hostil_nao_casa_ancora():
    sess = f"{DIR}/.cademi-session.json"
    a = _c("cademi", ALFA, _CONTA_HOSTIL, sess)
    b = _c("cademi", VIRAL, "unauthorized", sess)
    with pytest.raises(RuntimeError) as e:
        _exec(a, b).disparar(a.url)
    assert "também serve a conta" in str(e.value) and _sem_ancora_de_morte(str(e.value))


def test_plataforma_hotmart_em_subdominio_do_hotmart_dispara():
    sp = FakeSpawn()
    url = "https://sub.hotmart.com/pt-br/club/x/products/9"
    _exec(_hot(url, "hotmart-principal"), spawn=sp).disparar(url)
    assert [c["cmd"][2] for c in sp.calls] == ["motor.cli"]


# DENTE POR HOST DA TRAVA: cada host cadastrado em `hosts`, declarado com a plataforma
# ERRADA, é recusado pela trava host -> plataforma (a mensagem é a dela: "host de <dono>").
# "kiwify"/"memberkit" não têm nenhuma outra checagem que os barre — sem o host na trava,
# spawnariam; "hotmart" seria barrado também pela regra do hotmart.com, por isso o dente
# confere a MENSAGEM da trava.
@pytest.mark.parametrize("url,dono", [
    (ALFA, "cademi"), (VIRAL, "cademi"), (RAMON, "cademi"),
    (LUANA, "entregadigital"), ("https://outratenant.entregadigital.app.br/",
                                "entregadigital")])
@pytest.mark.parametrize("errada", ["kiwify", "memberkit", "hotmart"])
def test_cada_host_da_trava_recusa_a_plataforma_errada(url, dono, errada):
    sp = FakeSpawn()
    with pytest.raises(RuntimeError) as e:
        _exec(_c(errada, url, "conta-x", f"{DIR}/.x-session.json"), spawn=sp).disparar(url)
    assert sp.calls == []
    assert str(e.value).startswith(f"host de {dono} com plataforma {errada!r} no YAML")
    assert _sem_ancora_de_morte(str(e.value))


@pytest.mark.parametrize("url,errada", [
    (LUANA, "cademi"), (ALFA, "entregadigital"), (VIRAL, "memberkit"),
    ("https://x.entregadigital.app.br/", "hotmart")])
def test_host_de_uma_plataforma_nova_com_outra_plataforma_e_recusado(url, errada):
    sp = FakeSpawn()
    with pytest.raises(RuntimeError):
        _exec(_c(errada, url, "conta-x", f"{DIR}/.x-session.json"), spawn=sp).disparar(url)
    assert sp.calls == []


def test_plataformas_vivas_nao_ganham_checagem_de_host():
    # aditivo: hotmart/memberkit em hosts deles seguem disparando como sempre.
    sp = FakeSpawn()
    mk = _c("memberkit", "https://minha.memberkit.com.br/", "mk", f"{DIR}/.mk.json")
    ex = _exec(_hot(C1, "h"), mk, spawn=sp)
    ex.disparar(C1)
    ex.disparar(mk.url)
    assert [c["cmd"][2] for c in sp.calls] == ["motor.cli", "motor.memberkit"]


# ==========================================================================
# 5) CREDENCIAL DO TENANT — obrigatória e exclusiva
# ==========================================================================
@pytest.mark.parametrize("plat,url,conta,url_env,sess_env,sess", NOVAS)
def test_sem_session_path_nao_dispara(plat, url, conta, url_env, sess_env, sess):
    # sem o arquivo do tenant no YAML o daemon não confere de quem é a credencial: no
    # default do motor (r9: um arquivo por tenant; ED anterior à rodada 9: UM arquivo
    # para todos), duas contas do mesmo tenant partilhariam a sessão sem a trava ver.
    sp = FakeSpawn()
    with pytest.raises(RuntimeError) as e:
        _exec(_c(plat, url, conta, ""), spawn=sp).disparar(url)
    assert sp.calls == []
    assert "session_path" in str(e.value) and _sem_ancora_de_morte(str(e.value))


def test_mesmo_arquivo_de_sessao_em_dois_tenants_e_recusado():
    compartilhado = f"{DIR}/.cademi-session.json"
    a = _c("cademi", ALFA, "cademi-alfaresearch", compartilhado)
    b = _c("cademi", VIRAL, "cademi-codigoviral", ".cademi-session.json")  # relativo = mesmo
    sp = FakeSpawn()
    ex = _exec(a, b, spawn=sp)
    for c in (a, b):
        with pytest.raises(RuntimeError) as e:
            ex.disparar(c.url)
        assert _sem_ancora_de_morte(str(e.value))
    assert sp.calls == []


def test_mesmo_arquivo_na_mesma_conta_em_dois_tenants_e_recusado():
    # a MESMA conta em dois tenants com UM storage_state: cookies de dois hosts no mesmo
    # arquivo e cada run regrava o do outro — a checagem é por (conta, HOST), não só conta.
    sess = f"{DIR}/.cademi-session.json"
    a = _c("cademi", ALFA, "cademi-unica", sess)
    b = _c("cademi", VIRAL, "cademi-unica", sess)
    sp = FakeSpawn()
    with pytest.raises(RuntimeError):
        _exec(a, b, spawn=sp).disparar(a.url)
    assert sp.calls == []


def test_mesma_conta_mesmo_host_mesmo_arquivo_dispara():
    # controle: duas entradas do MESMO tenant e MESMA conta partilham legitimamente a
    # sessão (a recusa acima não é por "arquivo repetido").
    sess = f"{DIR}/.cademi-session-membros.alfaresearch.com.br.json"
    a = _c("cademi", ALFA, "cademi-alfaresearch", sess)
    b = _c("cademi", ALFA + "area/vitrine/home", "cademi-alfaresearch", sess)
    sp = FakeSpawn()
    _exec(a, b, spawn=sp).disparar(a.url)
    assert [c["cmd"][2] for c in sp.calls] == ["motor.cademi"]


def test_mesmo_arquivo_em_duas_contas_do_mesmo_host_e_recusado():
    sess = f"{DIR}/.cademi-session-membros.alfaresearch.com.br.json"
    a = _c("cademi", ALFA, "cademi-alfa-1", sess)
    b = _c("cademi", ALFA + "area/vitrine/home", "cademi-alfa-2", sess)
    sp = FakeSpawn()
    with pytest.raises(RuntimeError):
        _exec(a, b, spawn=sp).disparar(a.url)
    assert sp.calls == []


def test_arquivo_de_sessao_de_outra_plataforma_nao_serve_ao_tenant_novo():
    # o Cademí apontando o storage_state do Memberkit: o Cademí é recusado; o Memberkit
    # (spec sem `sessao_por_tenant`) segue disparando — nada muda para a viva.
    mk_sess = f"{DIR}/.memberkit-session.json"
    mk = _c("memberkit", "https://comunidade-triade.memberkit.com.br/", "memberkit-triade",
            mk_sess)
    cd = _c("cademi", ALFA, "cademi-alfaresearch", mk_sess)
    sp = FakeSpawn()
    ex = _exec(mk, cd, spawn=sp)
    with pytest.raises(RuntimeError):
        ex.disparar(cd.url)
    ex.disparar(mk.url)
    assert [c["cmd"][2] for c in sp.calls] == ["motor.memberkit"]


def test_arquivos_distintos_por_tenant_disparam_todos():
    sp = FakeSpawn()
    cursos = [_c(p, u, c, s) for (p, u, c, _ue, _se, s) in NOVAS]
    ex = _exec(*cursos, spawn=sp)
    for c in cursos:
        ex.disparar(c.url)
    envs = [call["env"].get("CADEMI_SESSION_PATH") or
            call["env"]["ENTREGADIGITAL_SESSION_PATH"] for call in sp.calls]
    assert envs == [s for (*_x, s) in NOVAS]


# ==========================================================================
# 6) TRACKER — pendências/terminais pelos course_id namespaced do tenant
# ==========================================================================
def _tracker(tmp_path):
    """{motor_dir}/tracker.db com o schema REAL do motor (motor/tracker.py)."""
    con = sqlite3.connect(str(tmp_path / "tracker.db"))
    con.execute("CREATE TABLE courses(course_id TEXT PRIMARY KEY, url TEXT, slug TEXT, "
                "name TEXT)")
    con.execute("""CREATE TABLE lessons(
        hash TEXT PRIMARY KEY, course_id TEXT, module TEXT, title TEXT,
        order_idx INTEGER, url TEXT, status TEXT DEFAULT 'pendente',
        notion_page_id TEXT, error TEXT, updated_at REAL)""")
    con.commit()
    con.close()
    return str(tmp_path)


def _seed(md, course_id, *statuses, error=None):
    con = sqlite3.connect(f"{md}/tracker.db")
    con.execute("INSERT OR IGNORE INTO courses(course_id) VALUES(?)", (course_id,))
    for s in statuses:
        con.execute("INSERT INTO lessons(hash,course_id,status,error,updated_at) "
                    "VALUES(?,?,?,?,?)",
                    (f"{course_id}#{time.monotonic_ns()}", course_id, s, error, time.time()))
    con.commit()
    con.close()


def _status(md, course_id):
    con = sqlite3.connect(f"{md}/tracker.db")
    r = sorted(s for (s,) in con.execute(
        "SELECT status FROM lessons WHERE course_id=?", (course_id,)))
    con.close()
    return r


def test_cademi_soma_o_tenant_inteiro_e_so_ele(tmp_path):
    md = _tracker(tmp_path)
    _seed(md, "cademi:membros.alfaresearch.com.br:10",
          "no_notion", "no_notion", "sem_audio", "pendente")
    _seed(md, "cademi:membros.alfaresearch.com.br:11", "transcrevendo")
    # vizinhos que NÃO podem entrar na conta:
    _seed(md, "cademi:cursos.codigoviral.com.br:5", "pendente", "pendente")   # outro tenant
    _seed(md, "cademi:membros.alfaresearch.com.br.evil:1", "pendente")        # prefixo sem ':'
    _seed(md, "CADEMI:MEMBROS.ALFARESEARCH.COM.BR:9", "pendente")             # LIKE ignoraria caixa
    _seed(md, "10", "pendente", "pendente", "pendente")                       # Hotmart product 10
    # 1 pendente + 1 transcrevendo (sem_audio é TERMINAL no Cademí)
    assert captura.pendencia_capturavel_local(ALFA, md, plataforma="cademi") == 2
    assert captura.pendencia_capturavel_local(VIRAL, md, plataforma="cademi") == 2
    # sem a plataforma (o chamador antigo) o leitor não conhece o tenant: desconhecido
    assert captura.pendencia_capturavel_local(ALFA, md) is None


def test_cademi_tenant_so_com_terminais_e_pronto(tmp_path):
    md = _tracker(tmp_path)
    _seed(md, "cademi:membros.alfaresearch.com.br:10",
          "no_notion", "sem_audio", "falhou", "audio_erro")
    assert captura.pendencia_capturavel_local(ALFA, md, plataforma="cademi") == 0


def test_cademi_sem_audio_legado_conta_como_trabalho(tmp_path):
    # o run do Cademí re-enfileira as `sem_audio` com a assinatura LEGADA; contá-las como
    # terminais deixaria o tenant 'pronto' e o resgate nunca rodaria.
    md = _tracker(tmp_path)
    _seed(md, "cademi:membros.alfaresearch.com.br:10", "no_notion")
    _seed(md, "cademi:membros.alfaresearch.com.br:10", "sem_audio",
          error="AudioUnavailable: nenhum player Panda capturável na aula x")
    _seed(md, "cademi:membros.alfaresearch.com.br:10", "sem_audio",
          error="nenhum player Panda/ScaleUp/Vimeo capturável na aula y")     # atual: terminal
    assert captura.pendencia_capturavel_local(ALFA, md, plataforma="cademi") == 1


def test_cademi_curso_conhecido_sem_aula_nao_e_pronto(tmp_path):
    # achado C3: o curso cujo 302 caiu numa página de erro entra em `courses` com 0 aulas;
    # o tenant NÃO é 'pronto' com um curso inteiro por capturar.
    md = _tracker(tmp_path)
    _seed(md, "cademi:membros.alfaresearch.com.br:10", "no_notion", "no_notion")
    _seed(md, "cademi:membros.alfaresearch.com.br:12")                # curso sem aula
    assert captura.pendencia_capturavel_local(ALFA, md, plataforma="cademi") is None


def test_tenant_nunca_semeado_e_desconhecido(tmp_path):
    md = _tracker(tmp_path)
    _seed(md, "cademi:cursos.codigoviral.com.br:5", "no_notion")
    assert captura.pendencia_capturavel_local(ALFA, md, plataforma="cademi") is None
    assert captura.pendencia_capturavel_local(LUANA, md,
                                              plataforma="entregadigital") is None


def test_entregadigital_soma_os_produtos_do_tenant(tmp_path):
    md = _tracker(tmp_path)
    _seed(md, "entregadigital:luanacarolina:product:7",
          "no_notion", "sem_audio", "sem_conteudo", "nao_video_erro",
          "capturando_nao_video", "pendente")
    _seed(md, "entregadigital:luanacarolina:product:8", "no_notion")
    _seed(md, "entregadigital:outra:product:7", "pendente", "pendente")        # outro tenant
    _seed(md, "entregadigital:luanacarolina2:product:1", "pendente")           # prefixo sem ':'
    assert captura.pendencia_capturavel_local(
        LUANA, md, plataforma="entregadigital") == 2


@pytest.mark.parametrize("url,prefixo", [
    (LUANA, "entregadigital:luanacarolina:product:"),
    ("https://LuanaCarolina.EntregaDigital.app.br/products/9",
     "entregadigital:luanacarolina:product:"),
    ("https://membros.cliente.com/", "entregadigital:membros.cliente.com:product:"),
    ("sem-host", None),
    ("http://[::1", None),                                 # malformada: sem escopo, sem estouro
])
def test_prefixo_entregadigital_espelha_o_tenant_do_motor(url, prefixo):
    assert captura._prefixo_entregadigital(url) == prefixo


@pytest.mark.parametrize("url,prefixo", [
    (ALFA, "cademi:membros.alfaresearch.com.br:"),
    ("https://Cursos.CodigoViral.com.br/area/vitrine/home",
     "cademi:cursos.codigoviral.com.br:"),
    ("https://www.x.com.br/", "cademi:www.x.com.br:"),     # tenant_of do motor mantém o www.
    ("http://[::1", None),
])
def test_prefixo_cademi_espelha_o_tenant_do_motor(url, prefixo):
    assert captura._prefixo_cademi(url) == prefixo


def test_entregadigital_url_com_products_nao_le_o_curso_hotmart_de_mesmo_numero(tmp_path):
    md = _tracker(tmp_path)
    _seed(md, "entregadigital:luanacarolina:product:123", "no_notion")
    _seed(md, "123", "pendente", "pendente")                                   # Hotmart 123
    url = LUANA + "products/123"
    assert captura.pendencia_capturavel_local(url, md, plataforma="entregadigital") == 0
    # o leitor antigo (sem plataforma) leria o Hotmart 123 — é o que o escopo evita
    assert captura.pendencia_capturavel_local(url, md) == 2


def test_hotmart_le_exatamente_como_antes(tmp_path):
    md = _tracker(tmp_path)
    _seed(md, "111", "no_notion", "pendente", "sem_audio")
    _seed(md, "111", "nao_video_erro",
          error="provedor não suportado: youtube (link externo)")
    url = "https://hotmart.com/pt-br/x/products/111"
    # sem_audio NÃO é terminal no leitor do Hotmart (viés a não-pronto) + resgate YouTube
    assert captura.pendencia_capturavel_local(url, md) == 3
    assert captura.pendencia_capturavel_local(url, md, plataforma="hotmart") == 3


def test_reaper_nao_toca_tenant_nem_o_hotmart_de_mesmo_numero(tmp_path):
    md = _tracker(tmp_path)
    _seed(md, "123", "transcrevendo")                      # Hotmart 123, talvez VIVO
    _seed(md, "cademi:membros.alfaresearch.com.br:10", "transcrevendo")
    url_ed = LUANA + "products/123"
    assert captura.reap_orphans_local(url_ed, md, curso_ativo=lambda u: False,
                                      plataforma="entregadigital") == 0
    assert captura.reap_orphans_local(ALFA, md, curso_ativo=lambda u: False,
                                      plataforma="cademi") == 0
    assert _status(md, "123") == ["transcrevendo"]
    assert _status(md, "cademi:membros.alfaresearch.com.br:10") == ["transcrevendo"]
    # sem a plataforma, a URL da Entrega Digital com /products/123 resetaria o Hotmart
    assert captura.reap_orphans_local(url_ed, md, curso_ativo=lambda u: False) == 1


def test_entrada_nao_declarada_nao_le_nem_reapa_o_hotmart_de_mesmo_numero(tmp_path):
    # entrada sem `plataforma:` fora do hotmart.com: dono desconhecido. Nem a pendência
    # (desconhecida => None) nem o reaper (no-op) usam o `/products/123` da URL — que é o
    # curso Hotmart 123, de outra conta, talvez VIVO.
    md = _tracker(tmp_path)
    _seed(md, "123", "transcrevendo", "pendente")
    url = NOVO_TENANT + "products/123"
    nd = captura.PLATAFORMA_NAO_DECLARADA
    assert captura.pendencia_capturavel_local(url, md, plataforma=nd) is None
    assert captura.reap_orphans_local(url, md, curso_ativo=lambda u: False,
                                      plataforma=nd) == 0
    assert _status(md, "123") == ["pendente", "transcrevendo"]
    # controle: como "hotmart" (o default antigo) a MESMA URL lia e resetava o 123
    assert captura.pendencia_capturavel_local(url, md, plataforma="hotmart") == 2
    assert captura.reap_orphans_local(url, md, curso_ativo=lambda u: False,
                                      plataforma="hotmart") == 1


# --- fiação de PRODUÇÃO (o que o main() injeta) ---------------------------------------
def test_pendencia_de_producao_passa_a_plataforma_do_yaml(tmp_path):
    md = _tracker(tmp_path)
    _seed(md, "cademi:membros.alfaresearch.com.br:10", "pendente", "no_notion")
    _seed(md, "entregadigital:luanacarolina:product:7", "no_notion")
    cursos = [_c("cademi", ALFA, "cademi-alfaresearch"),
              _c("entregadigital", LUANA, "entregadigital-luanacarolina")]
    fn = athena_local._pendencia_de_producao(cursos, lambda url: md)
    assert fn(ALFA) == 1
    assert fn(LUANA) == 0


def test_reaper_de_boot_de_producao_passa_a_plataforma(tmp_path):
    md = _tracker(tmp_path)
    _seed(md, "123", "transcrevendo")
    url_ed = LUANA + "products/123"
    cursos = [_c("entregadigital", url_ed, "entregadigital-luanacarolina",
                 f"{DIR}/.entregadigital-luanacarolina-session.json")]
    ex = _exec(*cursos)
    athena_local._reaper_de_boot(cursos, ex, lambda url: md)()
    assert _status(md, "123") == ["transcrevendo"]          # o Hotmart 123 ficou intacto


# ==========================================================================
# 7) PROGRESSO NO NOTION — a Origem da Entrega Digital não mora sob a URL do YAML
# ==========================================================================
def _run_que_conta(n, prefixos):
    def fake_run(cmd, *, cwd):
        prefixos.append(cmd[-1])                           # o prefixo de Origem vai como argv
        return f"{captura.PROGRESSO_SENTINELA} {n}\n"
    return fake_run


def test_prefixo_de_origem_da_entregadigital_e_o_share_url_do_tenant():
    assert captura.prefixo_origem_notion(LUANA, "entregadigital") == \
        "https://luanacarolina.appmagic.link/"
    assert captura.prefixo_origem_notion(LUANA + "products/9", "entregadigital") == \
        "https://luanacarolina.appmagic.link/"
    # Cademí e as vivas: a Origem já mora sob a URL do YAML => nada muda (None)
    assert captura.prefixo_origem_notion(ALFA, "cademi") is None
    assert captura.prefixo_origem_notion(C1, "hotmart") is None
    assert captura.prefixo_origem_notion("sem-host", "entregadigital") is None


def test_progresso_de_producao_conta_a_entregadigital_pela_origem_real():
    cursos = [_c("cademi", ALFA, "cademi-alfaresearch"),
              _c("entregadigital", LUANA, "entregadigital-luanacarolina"),
              _hot(C1, "h")]
    origem = athena_local.origem_notion_por_curso(cursos)
    assert origem == {LUANA: "https://luanacarolina.appmagic.link/"}
    prefixos = []
    fn = athena_local.progresso_local_fn("/py", "/dir", {}, run=_run_que_conta(5, prefixos),
                                         origem_por_curso=origem)
    assert fn(LUANA) == (5, 0) and fn(ALFA) == (5, 0) and fn(C1) == (5, 0)
    assert prefixos == ["https://luanacarolina.appmagic.link/",   # a Origem real da ED
                        "https://membros.alfaresearch.com.br/",   # Cademí: a URL do YAML
                        C1 + "/"]                                 # Hotmart: como sempre


def test_sem_o_mapa_a_entregadigital_contaria_pela_url_do_yaml():
    # o defeito que o mapa conserta: pelo prefixo do YAML, nenhuma aula da ED casa.
    prefixos = []
    athena_local.progresso_local_fn("/py", "/dir", {}, run=_run_que_conta(0, prefixos))(LUANA)
    assert prefixos == ["https://luanacarolina.entregadigital.app.br/"]


def test_share_host_e_o_do_motor():
    fonte = None
    for raiz in _ARVORES_MOTOR:
        p = os.path.join(raiz, "motor", "entregadigital", "enumerate.py")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                fonte = f.read()
            break
    if fonte is None:
        pytest.skip("nenhuma árvore do motor com motor/entregadigital/enumerate.py")
    assert f'SHARE_HOST = "{captura._ED_SHARE_HOST}"' in fonte
    assert 'f"https://{tenant}.{SHARE_HOST}/products/' in fonte


def test_main_fia_as_tres_leituras_por_plataforma():
    # o main() é I/O real (sem cobertura de execução: tocaria ~/.athena-local, a chave do
    # Groq e o Telegram). A fiação é conferida no FONTE: sem estas três linhas, as
    # fábricas testadas acima existiriam e o daemon não as usaria.
    import inspect
    fonte = inspect.getsource(athena_local.main)
    assert "pendencia_fn = _pendencia_de_producao(cursos, _motor_dir_do_curso)" in fonte
    assert "reaper_fn = _reaper_de_boot(cursos, executor, _motor_dir_do_curso)" in fonte
    assert "origem_por_curso=origem_notion_por_curso(cursos)" in fonte
    assert "captura.pendencia_capturavel_local(url, _motor_dir_do_curso(url))" not in fonte
