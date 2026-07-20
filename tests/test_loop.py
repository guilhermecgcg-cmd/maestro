from maestro.loop import ciclo
from maestro.acesso import Servico
from maestro.registro import Projeto


class _Acesso:
    def __init__(self, servicos): self._s = servicos; self.restarts = []; self.redeploys = []; self.logs_lidos = []
    def servicos(self): return self._s
    def saude_http(self, alvos): return {}
    def recursos(self): return {"disco_pct": 10, "ram_pct": 10}
    def logs(self, nome, n=50): self.logs_lidos.append(nome); return "OOM..."
    def restart(self, nome): self.restarts.append(nome)
    def redeploy(self, nome, proj): self.redeploys.append((nome, proj))


class _Voz:
    def __init__(self): self.avisos = []; self.escaladas = []
    def avisar_acao(self, a): self.avisos.append(a)
    def escalar(self, p, pedido): self.escaladas.append(pedido)


def _proj(**kw):
    base = dict(nome="p1", projeto_easypanel="proj1", servicos=("api",), saude={}, adaptador="", gerenciar=True)
    base.update(kw); return Projeto(**base)


def test_ciclo_conserta_servico_caido_e_avisa():
    a = _Acesso({"api": Servico("api", up=False, restarting=False)}); v = _Voz()
    ciclo(a, v, [_proj()], llm=lambda p: '{"acao":"nada","escalar":true,"diagnostico":""}')
    assert "api" in a.restarts and v.avisos


def test_restart_loop_cerebro_manda_redeploy_com_projeto_certo():
    a = _Acesso({"api": Servico("api", up=False, restarting=True)}); v = _Voz()
    ciclo(a, v, [_proj()], llm=lambda p: '{"acao":"redeploy","escalar":false,"diagnostico":"OOM"}')
    assert a.logs_lidos == ["api"] and ("api", "proj1") in a.redeploys


def test_restart_loop_llm_incerto_escala():
    a = _Acesso({"api": Servico("api", up=False, restarting=True)}); v = _Voz()
    ciclo(a, v, [_proj()], llm=lambda p: '{"acao":"nada","escalar":true,"diagnostico":"?"}')
    assert a.restarts == [] and a.redeploys == [] and v.escaladas


def test_so_olha_servicos_do_projeto():
    a = _Acesso({"api": Servico("api", up=True, restarting=False),
                 "outro": Servico("outro", up=False, restarting=False)}); v = _Voz()
    ciclo(a, v, [_proj(servicos=("api",))], llm=lambda p: "{}")
    assert a.restarts == []  # 'outro' está caído mas NÃO é do projeto -> ignora


def test_projeto_monitorado_so_avisa_nao_age():
    a = _Acesso({"api": Servico("api", up=False, restarting=False)}); v = _Voz()
    ciclo(a, v, [_proj(gerenciar=False)], llm=lambda p: "{}")
    assert a.restarts == [] and a.redeploys == []   # NÃO agiu
    assert v.escaladas                              # só avisou


# --- Camada 1: observação plugada no ciclo (opt-in via `db`) ----------------
from tests.test_observador import FakeDB


def test_ciclo_persiste_observacao_quando_db_injetado():
    a = _Acesso({"api": Servico("api", up=True, restarting=False)}); v = _Voz()
    db = FakeDB()
    ciclo(a, v, [_proj(saude={"api": "http://x/health"})],
          llm=lambda p: "{}", db=db)
    assert db.store["criada"] >= 1                       # tabela criada
    assert any(r[1] == "api" for r in db.store["rows"])  # snapshot gravado


def test_ciclo_sem_db_nao_observa_retrocompativel():
    a = _Acesso({"api": Servico("api", up=True, restarting=False)}); v = _Voz()
    # sem db -> nada de observação; loop idêntico ao deployado (não levanta)
    ciclo(a, v, [_proj()], llm=lambda p: "{}")
    assert v.avisos == [] and v.escaladas == []     # saudável + sem db: silêncio


def test_captura_vazia_do_adaptador_escala_sem_agir():
    # o loop precisa ROTEAR 'captura_vazia' pro adaptador conhecimento (senão cai
    # no playbook genérico como 'não-mapeado'); o adaptador escala sem agir.
    from maestro.playbook import Acao
    class _AcessoAd(_Acesso):
        def exec_sql(self, container, sql, *, db, user="postgres", rows=True):
            if "estado_aulas" in sql:
                return ["1978824|3|0"]
            return []
    a = _AcessoAd({"worker": Servico("worker", up=True, restarting=False)}); v = _Voz()
    proj = _proj(servicos=("worker",), adaptador="conhecimento",
                 db_container="cp_db", db_name="conhecimento")
    ciclo(a, v, [proj], llm=lambda p: "{}")
    assert a.restarts == [] and a.redeploys == []          # NÃO agiu sozinho
    assert any("falso-sucesso" in e for e in v.escaladas)  # escalou claro


class _AcessoContado(_Acesso):
    """Conta as sondas de /health e df/free e devolve saúde por-nome (modela o
    mecanismo real: saude_http fatiável por alvo, recursos global)."""
    def __init__(self, servicos, saude_map=None):
        super().__init__(servicos)
        self._saude_map = saude_map or {}
        self.saude_calls = 0
        self.recursos_calls = 0
        self.saude_alvos = []

    def saude_http(self, alvos):
        self.saude_calls += 1
        self.saude_alvos.append(dict(alvos))
        return {n: self._saude_map.get(n, False) for n in alvos}

    def recursos(self):
        self.recursos_calls += 1
        return {"disco_pct": 10, "ram_pct": 10}


def test_ciclo_nao_sonda_saude_recursos_em_dobro_TEETH():
    # TEETH (achado [7]): com db ligado e N projetos, o /health e o df/free eram
    # sondados em DOBRO — o observador sondava e o laço por-projeto sondava de novo os
    # MESMOS endpoints (1 na coletar + N por-projeto). Com a sonda única por ciclo,
    # saude_http roda UMA vez (sobre a união) e recursos() UMA vez. No código velho
    # este teste vê 3 e 3 (1 união + 2 projetos), não 1 e 1.
    a = _AcessoContado({"api": Servico("api", up=True, restarting=False),
                        "web": Servico("web", up=True, restarting=False)},
                       saude_map={"api": True, "web": False})
    v = _Voz()
    db = FakeDB()
    p1 = _proj(nome="p1", servicos=("api",), saude={"api": "http://a/health"})
    p2 = _proj(nome="p2", servicos=("web",), saude={"web": "http://w/health"})
    ciclo(a, v, [p1, p2], llm=lambda p: "{}", db=db)
    assert a.saude_calls == 1        # UMA sonda de /health no ciclo (não 1+N)
    assert a.recursos_calls == 1     # UMA leitura de df/free no ciclo (não 1+N)
    # a sonda única cobriu a UNIÃO dos alvos (cada projeto fatia o seu depois)
    assert a.saude_alvos[0] == {"api": "http://a/health", "web": "http://w/health"}
    # SÉRIE INTACTA: o observador gravou os dois serviços com o health CERTO reusado
    linhas = {r[1]: r for r in db.store["rows"]}          # servico -> (ts, sv, up, health, det)
    assert {"api", "web"} <= set(linhas)
    assert linhas["api"][3] is True and linhas["web"][3] is False


def test_ciclo_reusa_saude_doente_ainda_detecta_por_projeto_TEETH():
    # TEETH da CORRETUDE: o valor de /health reusado (da sonda única) tem de chegar ao
    # checar POR-PROJETO — o dedup não pode cegar a doença. Serviço Up-mas-doente
    # (health False) deve ser detectado (o loop lê os logs dele pra diagnosticar).
    a = _AcessoContado({"api": Servico("api", up=True, restarting=False)},
                       saude_map={"api": False})          # Up mas /health falhou
    v = _Voz()
    ciclo(a, v, [_proj(servicos=("api",), saude={"api": "http://a/health"})],
          llm=lambda p: '{"acao":"nada","escalar":true,"diagnostico":"?"}')
    assert "api" in a.logs_lidos      # doente detectado via saude reusada -> diagnosticou
    assert a.saude_calls == 1         # e sondou o /health só uma vez


def test_loop_coordena_cursos_desejados_por_url():
    # C4 wiring: o loop itera cursos_desejados do PROJETO conhecimento e dispara a
    # captura por course_url (sem criar entrada nova no registro), guardando estado
    # por-curso entre ciclos num dict aninhado sob a chave do projeto.
    class _AcessoCap(_Acesso):
        def __init__(self, s): super().__init__(s); self.sqls = []
        def exec_sql(self, container, sql, *, db, user="postgres", rows=True):
            self.sqls.append(sql)
            return []                                       # sem sinal de morte / fila limpa
        def exec_app(self, container, comando, timeout=None):
            # guard anti-duplicidade: Notion vazio p/ este curso -> (novo) -> enfileira.
            from maestro.adaptadores import captura
            if captura.NOTION_SENTINELA in comando:
                return f"{captura.NOTION_SENTINELA} 0\n"
            return "RECONCILE_OK {}\n"
    a = _AcessoCap({"worker": Servico("worker", up=True, restarting=False)}); v = _Voz()
    # app_container presente: o guard anti-duplicidade da captura consulta o Notion de
    # dentro do container do app antes de enfileirar (curso novo aqui -> segue).
    proj = _proj(servicos=("worker",), adaptador="conhecimento", db_container="cp_db",
                 db_name="conhecimento", app_container="cp_app",
                 cursos_desejados=("https://plat/c1",))
    estado = {}
    ciclo(a, v, [proj], llm=lambda p: "{}", estado=estado)
    # enfileirou o curso desejado por course_url e persistiu a fase
    assert any("INSERT INTO fila_captura" in s and "https://plat/c1" in s for s in a.sqls)
    assert estado["p1::captura"]["https://plat/c1"]["fase"] == "capturando"


# --- CAMADA 2: orquestrador ligado ao ciclo (voo reinjetado cross-ciclo) -----
import asyncio

from maestro import loop as loopmod

URL_ORQ = "https://plat/orq1"


class _AcessoOrq(_Acesso):
    """Modela o MECANISMO real do caminho orquestrador: o Notion (exec_app,
    PROGRESSO_NOTION) reporta `no_notion`; o tracker (exec_sql) resolve course_id e
    total esperado; o INSERT na fila é o enfileiramento da passada. `no_notion` é
    mutado ENTRE ciclos (o worker residencial avançando a captura no Notion)."""
    def __init__(self, s, no_notion=10, total=18):
        super().__init__(s)
        self.no_notion = no_notion
        self.total = total
        self.sqls = []
        self.inserts = 0

    def exec_sql(self, container, sql, *, db, user="postgres", rows=True):
        self.sqls.append(sql)
        if "INSERT INTO fila_captura" in sql:
            self.inserts += 1
            return []
        if "coalesce(course_id" in sql:
            return ["cid1"]
        if "estado_aulas" in sql:
            return [f"{self.total}|0"]
        return []

    def exec_app(self, container, comando, timeout=None):
        from maestro.adaptadores import captura
        if captura.PROGRESSO_SENTINELA in comando:
            return f"{captura.PROGRESSO_SENTINELA} {self.no_notion}\n"
        return "RECONCILE_OK {}\n"       # rotina de reconcile do projeto conhecimento


def _proj_orq():
    return _proj(servicos=("worker",), adaptador="conhecimento", db_container="cp_db",
                 db_name="conhecimento", app_container="cp_app",
                 cursos_desejados=(URL_ORQ,))


def _cursos_de(resultados):
    return [r for r in resultados if getattr(r, "divergencia", None) is not None]


def test_orquestrador_cursos_voo_reinjetado_confirma_cross_ciclo_TEETH():
    # DENTES do CONTRATO 1: com o MESMO `voo` reinjetado, uma passada enfileirada num
    # ciclo (Notion 10/18 -> em voo, SEM escalar) é CONFIRMADA num ciclo POSTERIOR
    # quando o worker avança o Notion para 18/18. Prova que o loop REALMENTE invoca a
    # Camada 2 e que a confirmação é cross-ciclo — nunca no ciclo do disparo.
    a = _AcessoOrq({"worker": Servico("worker", up=True, restarting=False)})
    v = _Voz()
    proj = _proj_orq()
    voo = {}                                   # criado UMA vez, reinjetado nos dois ciclos
    r1 = ciclo(a, v, [proj], llm=lambda p: "{}", voo=voo, orquestrar_cursos=True)
    c1 = _cursos_de(r1)
    assert c1 and c1[0].em_voo is True and c1[0].escalou is False
    assert URL_ORQ in voo                       # a passada persiste "em voo"
    assert a.inserts == 1                        # enfileirou UMA vez
    assert v.escaladas == []                     # NÃO escalou no ciclo do disparo

    a.no_notion = 18                             # worker residencial avança o Notion
    r2 = ciclo(a, v, [proj], llm=lambda p: "{}", voo=voo, orquestrar_cursos=True)
    c2 = _cursos_de(r2)
    assert c2 and c2[0].confirmado is True and c2[0].em_voo is False
    assert URL_ORQ not in voo                    # saiu do voo ao confirmar
    assert a.inserts == 1                        # NÃO re-enfileirou (anti-ban: não martela)


def test_orquestrador_cursos_voo_NAO_reinjetado_nunca_confirma_TEETH():
    # DENTES da REINJEÇÃO: se o loop criasse um `voo` NOVO a cada ciclo (não reinjetasse
    # o mesmo dict), a passada em voo se perderia e a confirmação cross-ciclo NUNCA
    # aconteceria. Aqui cada ciclo recebe um voo FRESCO -> o ciclo 2 (já completo no
    # Notion) não confirma nada (a passada some). É o modo de falha que a reinjeção mata.
    a = _AcessoOrq({"worker": Servico("worker", up=True, restarting=False)})
    v = _Voz()
    proj = _proj_orq()
    ciclo(a, v, [proj], llm=lambda p: "{}", voo={}, orquestrar_cursos=True)  # voo efêmero
    a.no_notion = 18
    r2 = ciclo(a, v, [proj], llm=lambda p: "{}", voo={}, orquestrar_cursos=True)  # outro voo
    c2 = _cursos_de(r2)
    assert not any(r.confirmado for r in c2)     # sem reinjeção: confirmação se perde


def test_ciclo_desligado_por_default_usa_coordenar_nao_orquestrador():
    # RETROCOMPAT + fail-closed: orquestrar_cursos default False -> o caminho vivo é o
    # coordenar (com anti-dup/sessão/ingest), NÃO o orquestrador. O guard anti-dup do
    # coordenar consulta o Notion (JA_NO_NOTION) antes de enfileirar.
    class _AcessoCoord(_AcessoOrq):
        def exec_app(self, container, comando, timeout=None):
            from maestro.adaptadores import captura
            if captura.NOTION_SENTINELA in comando:
                return f"{captura.NOTION_SENTINELA} 0\n"      # curso novo -> enfileira
            return "RECONCILE_OK {}\n"
    a = _AcessoCoord({"worker": Servico("worker", up=True, restarting=False)})
    v = _Voz()
    estado = {}
    ciclo(a, v, [_proj_orq()], llm=lambda p: "{}", estado=estado)  # orquestrar_cursos=False
    # coordenar rodou: consultou o guard anti-dup (JA_NO_NOTION) e persistiu a fase
    assert estado["p1::captura"][URL_ORQ]["fase"] == "capturando"


def test_run_reinjeta_voo_entre_ciclos_e_nao_martela_TEETH():
    # DENTES da PLUMBAGEM em run(): run cria o voo UMA vez e o REINJETA a cada ciclo.
    # ANTI-BAN: um curso que segue INCOMPLETO no Notion (o worker ainda captura) NÃO
    # pode ser re-enfileirado ciclo a ciclo — a passada fica "em voo" e AGUARDA (dentro
    # da janela de espera). Isso SÓ acontece se o mesmo `voo` sobrevive entre ciclos:
    # se run recriasse o voo a cada ciclo, cada ciclo trataria o curso como "1ª vez" e
    # RE-ENFILEIRARIA (martelar = violação anti-ban). 3 ciclos, 1 só INSERT = prova.
    a = _AcessoOrq({"worker": Servico("worker", up=True, restarting=False)})
    a.no_notion = 10                       # segue incompleto (10/18) em todos os ciclos
    v = _Voz()

    async def _noslp(_s):
        return

    asyncio.run(loopmod.run(a, v, [_proj_orq()], llm=lambda p: "{}", sleep=_noslp,
                            max_iters=3, orquestrar_cursos=True))
    assert a.inserts == 1                  # enfileirou UMA vez em 3 ciclos (não martelou)


# --- CAMADA 3: listener concorrente ao loop de saúde (servir) ----------------
def test_servir_entrelaca_loop_de_saude_e_listener_da_athena_TEETH():
    # DENTES do CONTRATO 2: servir roda a Camada 1/2 (loop de saúde) E a Camada 3
    # (listener de comandos da Athena) CONCORRENTES. Prova que ambos progridem no mesmo
    # servir: o serviço caído é reiniciado pelo loop de saúde E o /status autorizado é
    # atendido pelo listener, com autorizados + offset PLUMBADOS (fail-closed + persistido).
    from maestro.athena import Athena
    from maestro.telegram_api import Update

    class _FakeTG:
        def __init__(self, roteiro):
            self.enviadas = []
            self._r = list(roteiro)
            self.offsets = []
        def send_message(self, chat_id, texto, **kw):
            self.enviadas.append((chat_id, texto))
        def get_updates(self, offset, timeout=25):
            self.offsets.append(offset)
            return self._r.pop(0) if self._r else []

    a = _Acesso({"api": Servico("api", up=False, restarting=False)})
    v = _Voz()
    tg = _FakeTG([[Update(update_id=1, chat_id=100, texto="/status")]])
    ath = Athena(tg, v, acesso=a, autorizados={100}, captura_fn=lambda: "ok")
    store = {}

    async def _noslp(_s):
        return

    asyncio.run(loopmod.servir(
        a, v, [_proj()], llm=lambda p: "{}", athena=ath, sleep_saude=_noslp,
        intervalo_s=0, max_iters=1, athena_intervalo=0, athena_max_iters=2,
        offset_load=lambda: store.get("o", 0),
        offset_save=lambda o: store.__setitem__("o", o)))

    assert "api" in a.restarts                       # Camada 1/2 rodou (loop de saúde)
    assert any(c == 100 for (c, _) in tg.enviadas)   # Camada 3 rodou (atendeu /status)
    assert store.get("o") == 2                        # offset PLUMBADO e persistido


def test_servir_ignora_comando_de_chat_nao_autorizado_TEETH():
    # DENTES do fail-closed plumbado: um /restart de um chat FORA da whitelist não pode
    # virar ação nem sequer resposta (anti-reflector). Prova que servir preserva a trava
    # de autorização da Camada 3 ponta-a-ponta.
    from maestro.athena import Athena
    from maestro.telegram_api import Update

    class _FakeTG:
        def __init__(self, roteiro):
            self.enviadas = []
            self._r = list(roteiro)
        def send_message(self, chat_id, texto, **kw):
            self.enviadas.append((chat_id, texto))
        def get_updates(self, offset, timeout=25):
            return self._r.pop(0) if self._r else []

    a = _Acesso({"api": Servico("api", up=True, restarting=False)})
    v = _Voz()
    tg = _FakeTG([[Update(update_id=1, chat_id=66666, texto="/restart api")]])
    ath = Athena(tg, v, acesso=a, autorizados={100})  # 66666 não está na whitelist

    async def _noslp(_s):
        return

    asyncio.run(loopmod.servir(a, v, [_proj()], llm=lambda p: "{}", athena=ath,
                               sleep_saude=_noslp, intervalo_s=0, max_iters=1,
                               athena_intervalo=0, athena_max_iters=2))
    assert a.restarts == []          # comando não-autorizado NÃO reiniciou nada
    assert tg.enviadas == []         # nem respondeu (anti-reflector)
