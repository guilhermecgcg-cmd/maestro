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
    """Modela o MECANISMO real do caminho orquestrador AGORA que o EXECUTOR da passada
    é o `captura.coordenar` (a decisão do dono). Portanto o dublê responde a TODAS as
    consultas que o coordenar faz — não só ao INSERT da fila:
      - sessão MORTA (fila_captura status): [] => 'desconhecida' (segue, não loga).
      - anti-dup por COMPLETUDE (exec_app, JA_NO_NOTION): `no_notion` já no Notion.
      - progresso do tracker (estado_aulas): pend = total - no_notion (>0 => ainda
        capturando; 0 => tracker diz done, aí entra o gate I-1).
      - gate I-1 (exec_app, PROGRESSO_NOTION): `no_notion` — a completude-por-Notion.
      - auto-ingest (exec_app, reconcile): RECONCILE_OK.
    `no_notion` é mutado ENTRE ciclos (o worker residencial avançando no Notion)."""
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
            pend = max(self.total - self.no_notion, 0)     # pend reflete o incompleto
            return [f"{self.total}|{pend}"]
        return []                                          # sessao_morta etc.: sem sinal

    def exec_app(self, container, comando, timeout=None):
        from maestro.adaptadores import captura
        if captura.NOTION_SENTINELA in comando:            # anti-dup por completude
            return f"{captura.NOTION_SENTINELA} {self.no_notion}\n"
        if captura.PROGRESSO_SENTINELA in comando:         # gate I-1 / vigília de stall
            return f"{captura.PROGRESSO_SENTINELA} {self.no_notion}\n"
        return "RECONCILE_OK {}\n"                          # auto-ingest (reconcile)


def _proj_orq():
    return _proj(servicos=("worker",), adaptador="conhecimento", db_container="cp_db",
                 db_name="conhecimento", app_container="cp_app",
                 cursos_desejados=(URL_ORQ,))


def _cursos_de(resultados):
    # o owner devolve ResultadoCurso (curso + concluido/stall_escalado); filtra-os.
    return [r for r in resultados if getattr(r, "curso", None) is not None]


def test_orquestrador_cursos_owner_dirige_coordenar_e_confirma_por_notion_TEETH():
    # DENTES da DECISÃO DO DONO: o ORQUESTRADOR é o dono do disparo, mas o EXECUTOR da
    # passada é o coordenar (travas reusadas). Ciclo 1 (Notion 10/18): o coordenar
    # enfileira UMA vez (anti-dup por completude viu 10<18 -> RETOMA) e NÃO conclui; o
    # owner marca o curso em vigília no `voo`, sem escalar. Só num ciclo POSTERIOR, com o
    # Notion em 18/18, a conclusão é PROVADA (completude-por-Notion) — nunca no ciclo do
    # disparo, nunca por flag. TEETH: se o owner disparasse cru (sem coordenar), a
    # anti-dup/gate I-1 não correriam (nenhuma consulta JA_NO_NOTION/PROGRESSO_NOTION).
    a = _AcessoOrq({"worker": Servico("worker", up=True, restarting=False)})
    v = _Voz()
    proj = _proj_orq()
    voo = {}                                   # criado UMA vez, reinjetado nos dois ciclos
    r1 = ciclo(a, v, [proj], llm=lambda p: "{}", voo=voo, orquestrar_cursos=True)
    c1 = _cursos_de(r1)
    assert c1 and c1[0].concluido is False and c1[0].stall_escalado is False
    assert URL_ORQ in voo                       # curso em vigília (aguardando o Notion)
    assert a.inserts == 1                        # o EXECUTOR (coordenar) enfileirou UMA vez
    # a passada REALMENTE passou pelo coordenar: consultou anti-dup por completude
    assert any("estado_aulas" in s for s in a.sqls)  # (total esperado p/ a régua de completude)
    assert v.escaladas == []                     # NÃO escalou no ciclo do disparo

    a.no_notion = 18                             # worker residencial avança o Notion p/ 18/18
    r2 = ciclo(a, v, [proj], llm=lambda p: "{}", voo=voo, orquestrar_cursos=True)
    c2 = _cursos_de(r2)
    assert c2 and c2[0].concluido is True        # completude PROVADA no Notion (18>=18)
    assert URL_ORQ not in voo                    # saiu da vigília ao concluir
    assert a.inserts == 1                        # NÃO re-enfileirou (anti-ban: não martela)


def test_orquestrador_owner_enum_transitoria_zero_NAO_confirma_falso_TEETH():
    # DENTES do achado 'enum transitória=0 falsa-confirmação': se a enumeração do total
    # falha NESTE ciclo (course_id ainda não resolvido -> _total_esperado=0), o owner
    # NÃO pode declarar o curso concluído (total<=0 nunca prova completude-por-Notion) —
    # seria um falso-pronto. Deve apenas AGUARDAR. TEETH: no bug, total=0 tornava o curso
    # 'não incompleto' e a confirmação cega o dava por concluído.
    class _AcessoEnumZero(_AcessoOrq):
        def exec_sql(self, container, sql, *, db, user="postgres", rows=True):
            self.sqls.append(sql)
            if "INSERT INTO fila_captura" in sql:
                self.inserts += 1
                return []
            if "coalesce(course_id" in sql:
                return [""]            # worker ainda não resolveu o course_id
            if "estado_aulas" in sql:
                return []              # sem course_id -> enumeração vazia -> total=0
            return []
    a = _AcessoEnumZero({"worker": Servico("worker", up=True, restarting=False)})
    a.no_notion = 200                  # há aulas no Notion, mas o total é DESCONHECIDO
    v = _Voz()
    voo = {}
    r = ciclo(a, v, [_proj_orq()], llm=lambda p: "{}", voo=voo, orquestrar_cursos=True)
    c = _cursos_de(r)
    assert c and c[0].concluido is False         # total desconhecido NUNCA conclui
    assert URL_ORQ not in voo                     # sem denominador: nem vigia (só aguarda)


def test_orquestrador_owner_vigia_stall_e_escala_uma_vez_TEETH():
    # DENTES do valor da Camada 2 sobre o coordenar: o coordenar, uma vez capturando,
    # fica QUIETO para sempre — nunca reconhece que a captura EMPACOU. O owner vigia o
    # numerador no Notion e, se ele NÃO avança por `espera_s`, ESCALA uma vez (latch, sem
    # spam). Aqui o Notion trava em 10/18 por ciclos além da janela -> uma escalada de
    # estagnação; e o coordenar NÃO re-enfileira (anti-ban). TEETH: sem a vigília, um
    # curso empacado ficaria eternamente em silêncio (0 escaladas).
    a = _AcessoOrq({"worker": Servico("worker", up=True, restarting=False)})
    a.no_notion = 10                              # trava em 10/18 (worker não progride)
    v = _Voz()
    proj = _proj_orq()
    voo = {}
    # espera_s curta via override do orquestrador não é exposta pelo ciclo; simula-se com
    # muitos ciclos e uma janela default alta -> em vez disso testamos o owner direto:
    from maestro import orquestrador
    from maestro.adaptadores import captura
    executor = captura.FilaExecutor(a, proj)
    cap_estado = {}
    def passada(curso):
        return captura.coordenar(proj, a, v, executor=executor, curso_url=curso,
                                 estado=cap_estado.setdefault(curso, {}), agora=1000.0,
                                 progresso_notion_fn=lambda u: captura.progresso_curso_no_notion(a, "cp_app", u))
    def notion(curso):
        return (captura.progresso_curso_no_notion(a, "cp_app", curso).no_notion,
                captura._total_esperado(proj, a, curso, None))
    # ciclo 0 (t=1000): enfileira, entra em vigília
    orquestrador.orquestrar_captura([URL_ORQ], passada, notion, v, voo,
                                    agora=1000.0, espera_s=600.0)
    assert v.escaladas == []                      # ainda dentro da janela
    # ciclo 1 (t além da janela, sem avanço): ESCALA estagnação UMA vez
    r1 = orquestrador.orquestrar_captura([URL_ORQ], passada, notion, v, voo,
                                         agora=1000.0 + 601, espera_s=600.0)
    assert r1[0].stall_escalado is True
    assert sum("ESTAGNADA" in e for e in v.escaladas) == 1
    # ciclo 2 (ainda travado): latch -> NÃO re-escala (não spamma)
    orquestrador.orquestrar_captura([URL_ORQ], passada, notion, v, voo,
                                    agora=1000.0 + 1202, espera_s=600.0)
    assert sum("ESTAGNADA" in e for e in v.escaladas) == 1
    assert a.inserts == 1                          # o coordenar NÃO re-enfileirou (anti-ban)


def test_orquestrador_ON_gate_plataforma_nova_escala_e_NAO_captura_TEETH():
    # DENTES do achado IMPORTANTE da fiação: ligar o orquestrador BURLAVA o gate de
    # plataforma-nova (ele só existia no ramo coordenar). Agora o gate está TAMBÉM no
    # caminho do orquestrador: um curso Kiwify (sem adaptador) com orquestrar_cursos=True
    # é ESCALADO e NÃO capturado (o coordenar nem é chamado). TEETH: sem o gate no ramo
    # ON, o curso seria disparado numa plataforma que o motor não sabe raspar.
    a = _AcessoOrq({"worker": Servico("worker", up=True, restarting=False)})
    v = _Voz()
    proj = _proj(servicos=("worker",), adaptador="conhecimento", db_container="cp_db",
                 db_name="conhecimento", app_container="cp_app",
                 cursos_desejados=("https://app.kiwify.com/curso/9",))
    estado = {}
    ciclo(a, v, [proj], llm=lambda p: "{}", estado=estado, voo={}, orquestrar_cursos=True,
          plataformas_suportadas=frozenset({"hotmart.com"}))
    assert a.inserts == 0                              # NÃO enfileirou (não capturou)
    assert any("PLATAFORMA NOVA" in e for e in v.escaladas)   # escalou o gatilho de B
    # latch: 2º ciclo NÃO re-escala (não spamma)
    ciclo(a, v, [proj], llm=lambda p: "{}", estado=estado, voo={}, orquestrar_cursos=True,
          plataformas_suportadas=frozenset({"hotmart.com"}))
    assert sum("PLATAFORMA NOVA" in e for e in v.escaladas) == 1


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


def test_run_orquestrador_nao_martela_a_fila_em_3_ciclos_TEETH():
    # DENTES ANTI-BAN da PLUMBAGEM em run(): um curso que segue INCOMPLETO no Notion (o
    # worker ainda captura) NÃO pode ser re-enfileirado ciclo a ciclo. Com o coordenar
    # como EXECUTOR, a fase persiste no `estado` (por-curso, entre ciclos): ciclo 1
    # enfileira e vai a CAPTURANDO; ciclos 2-3 monitoram, NÃO re-enfileiram. A fase
    # persiste porque run mantém o MESMO `estado` entre ciclos. 3 ciclos, 1 só INSERT.
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


# --- CAPACIDADE B: gatilho de plataforma nova (detecta -> escala, não captura) ---
def test_plataforma_nova_escala_e_NAO_captura_TEETH():
    # DENTES: um curso desejado numa plataforma SEM adaptador (kiwify) não pode ser
    # capturado às cegas — o motor só sabe Hotmart. O gatilho da Capacidade B ESCALA
    # (uma vez) e PULA o curso; o coordenar (que enfileiraria) NÃO é chamado.
    class _AcessoB(_AcessoOrq):
        def __init__(self, s):
            super().__init__(s); self.coordenou = False
        def exec_app(self, container, comando, timeout=None):
            self.coordenou = True                       # qualquer toque no Notion = coordenar rodou
            return super().exec_app(container, comando, timeout)
    a = _AcessoB({"worker": Servico("worker", up=True, restarting=False)})
    v = _Voz()
    proj = _proj(servicos=("worker",), adaptador="conhecimento", db_container="cp_db",
                 db_name="conhecimento", app_container="cp_app",
                 cursos_desejados=("https://app.kiwify.com/curso/9",))
    estado = {}
    ciclo(a, v, [proj], llm=lambda p: "{}", estado=estado,
          plataformas_suportadas=frozenset({"hotmart.com"}))
    assert a.inserts == 0                              # NÃO enfileirou (não capturou)
    assert any("PLATAFORMA NOVA" in e for e in v.escaladas)   # escalou o gatilho de B
    # latch: um 2º ciclo NÃO re-escala (não spamma o operador)
    ciclo(a, v, [proj], llm=lambda p: "{}", estado=estado,
          plataformas_suportadas=frozenset({"hotmart.com"}))
    assert sum("PLATAFORMA NOVA" in e for e in v.escaladas) == 1


def test_plataforma_suportada_segue_para_coordenar():
    # Contraprova: um curso Hotmart (suportado) NÃO é escalado como plataforma nova —
    # segue para o coordenar normalmente (enfileira).
    class _AcessoCoord(_AcessoOrq):
        def exec_app(self, container, comando, timeout=None):
            from maestro.adaptadores import captura
            if captura.NOTION_SENTINELA in comando:
                return f"{captura.NOTION_SENTINELA} 0\n"   # curso novo -> enfileira
            return "RECONCILE_OK {}\n"
    a = _AcessoCoord({"worker": Servico("worker", up=True, restarting=False)})
    v = _Voz()
    proj = _proj(servicos=("worker",), adaptador="conhecimento", db_container="cp_db",
                 db_name="conhecimento", app_container="cp_app",
                 cursos_desejados=("https://hotmart.com/club/x/products/9",))
    ciclo(a, v, [proj], llm=lambda p: "{}", estado={},
          plataformas_suportadas=frozenset({"hotmart.com"}))
    assert not any("PLATAFORMA NOVA" in e for e in v.escaladas)   # não é plataforma nova
    assert a.inserts == 1                                          # coordenou (enfileirou)
