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


def test_loop_coordena_cursos_desejados_por_url():
    # C4 wiring: o loop itera cursos_desejados do PROJETO conhecimento e dispara a
    # captura por course_url (sem criar entrada nova no registro), guardando estado
    # por-curso entre ciclos num dict aninhado sob a chave do projeto.
    class _AcessoCap(_Acesso):
        def __init__(self, s): super().__init__(s); self.sqls = []
        def exec_sql(self, container, sql, *, db, user="postgres", rows=True):
            self.sqls.append(sql)
            return []                                       # sem sinal de morte / fila limpa
    a = _AcessoCap({"worker": Servico("worker", up=True, restarting=False)}); v = _Voz()
    # app_container vazio: a rotina reconcile do adaptador conhecimento nao dispara
    # (foco do teste = wiring da captura), mas o coordenar so precisa dela na conclusao.
    proj = _proj(servicos=("worker",), adaptador="conhecimento", db_container="cp_db",
                 db_name="conhecimento", app_container="",
                 cursos_desejados=("https://plat/c1",))
    estado = {}
    ciclo(a, v, [proj], llm=lambda p: "{}", estado=estado)
    # enfileirou o curso desejado por course_url e persistiu a fase
    assert any("INSERT INTO fila_captura" in s and "https://plat/c1" in s for s in a.sqls)
    assert estado["p1::captura"]["https://plat/c1"]["fase"] == "capturando"
