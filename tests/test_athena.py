"""Camada 3 (interface Telegram da Athena): ACEITA comandos e os despacha para a ação
certa; NOTIFICA proativamente só quando há decisão humana a tomar (anti-spam).

Disciplina dos dublês (memória do usuário: dublê que não modela o mecanismo é teatro):
  - TelegramClient -> dublê FakeTG (nada de rede); é o único seam de I/O.
  - Acesso -> REAL, com um `run_cmd` gravador. servicos() parseia `docker ps` de
    verdade; restart() emite o `docker restart` real; exec_sql() roda o wrapper real
    (sentinela __EXEC_OK__). Assim o teste pega a mecânica, não a coreografia.
  - Voz -> REAL, embrulhando o mesmo FakeTG (a escalação real é exercida).
  - FilaExecutor -> REAL: /capturar tem que produzir o INSERT real na fila_captura.
"""
import pytest

from maestro.athena import Athena, Evento, parse_comando
from maestro.voz import Voz, Comando
from maestro.acesso import Acesso
from maestro.adaptadores import captura
from maestro.registro import Projeto
from maestro.telegram_api import Update


# --- dublês -----------------------------------------------------------------
class FakeTG:
    """Único seam de rede. Grava o que sairia e serve updates roteirizados."""
    def __init__(self, roteiro=None):
        self.enviadas = []                 # (chat_id, texto)
        self._roteiro = list(roteiro or [])
        self.offsets = []                  # offsets pedidos ao get_updates
    def send_message(self, chat_id, texto, **kw):
        self.enviadas.append((chat_id, texto))
    def get_updates(self, offset, timeout=25):
        self.offsets.append(offset)
        return self._roteiro.pop(0) if self._roteiro else []


_PS = (
    "Up 3 hours  conhecimento_worker.1.aaa\n"
    "Restarting (1) 5s ago  conhecimento_api.1.bbb\n"
    "Exited (0) 2 min ago  conhecimento_db.1.ccc\n"
)


class Runner:
    """run_cmd gravador para o Acesso REAL. Distingue os comandos pelo conteúdo."""
    def __init__(self, ps=_PS):
        self.ps = ps
        self.calls = []
    def __call__(self, cmd, timeout=None):
        self.calls.append(cmd)
        if "docker ps -a --format" in cmd:
            return self.ps
        if "psql" in cmd:                  # wrapper do exec_sql -> sinaliza sucesso
            return "__EXEC_OK__\n"
        return ""


_PROJ = Projeto(nome="conhecimento", projeto_easypanel="conh",
                servicos=("worker", "api", "db"),
                db_container="conh_db", db_name="conh", db_user="postgres")


def _montar(roteiro=None, captura_fn=None, prioridades=None, executor="real",
            autorizados=(100,)):
    tg = FakeTG(roteiro)
    runner = Runner()
    acesso = Acesso(run_cmd=runner)
    voz = Voz(tg, [999])                    # 999 = chat do operador (broadcast)
    if executor == "real":
        executor = captura.FilaExecutor(acesso, _PROJ)
    ath = Athena(tg, voz, acesso=acesso, executor=executor,
                 captura_fn=captura_fn, prioridades=prioridades,
                 autorizados=autorizados)   # 100 = o operador que comanda nos testes
    return ath, tg, runner


def _para(tg, chat):
    return [t for (c, t) in tg.enviadas if c == chat]


# --- parser -----------------------------------------------------------------
def test_parse_reconhece_os_quatro_comandos():
    assert parse_comando("/status") == Comando("status", "")
    assert parse_comando("/capturar http://x/y") == Comando("capturar", "http://x/y")
    assert parse_comando("/restart worker") == Comando("restart", "worker")
    assert parse_comando("/prioridade") == Comando("prioridade", "")


def test_parse_ignora_sufixo_de_bot_em_grupo():
    # Telegram em grupo entrega "/status@MeuBot" — o verbo real é 'status'.
    assert parse_comando("/status@MaestroBot") == Comando("status", "")


def test_parse_sem_barra_ou_desconhecido():
    assert parse_comando("oi tudo bem").tipo == "desconhecido"
    assert parse_comando("/xpto foo").tipo == "desconhecido"


# --- /status ----------------------------------------------------------------
def test_status_reporta_servicos_e_captura_ao_remetente():
    ath, tg, runner = _montar(captura_fn=lambda: "capturando: curso-42")
    ath.atender(100, "/status")
    resp = _para(tg, 100)
    assert len(resp) == 1
    txt = resp[0]
    # estado REAL parseado do docker ps: up / restart-loop / caído
    assert "worker" in txt and "up" in txt.lower()
    assert "api" in txt and ("restart" in txt.lower())
    assert "db" in txt
    assert "capturando: curso-42" in txt          # captura_fn foi consultada
    # SOLICITADO: responde ao remetente, NÃO faz broadcast ao operador (999)
    assert _para(tg, 999) == []


# --- /capturar --------------------------------------------------------------
def test_capturar_enfileira_curso_de_verdade():
    url = "https://hotmart.com/club/x/products/9"
    ath, tg, runner = _montar()
    ath.atender(100, f"/capturar {url}")
    # O INSERT REAL na fila_captura tem que ter passado pelo run_cmd, com a URL.
    inserts = [c for c in runner.calls if "INSERT INTO fila_captura" in c and url in c]
    assert len(inserts) == 1
    assert _para(tg, 100)                          # confirma ao remetente
    assert _para(tg, 999) == []                    # sem broadcast


def test_capturar_sem_link_nao_enfileira():
    ath, tg, runner = _montar()
    ath.atender(100, "/capturar")
    assert [c for c in runner.calls if "INSERT INTO fila_captura" in c] == []
    assert "uso" in _para(tg, 100)[0].lower()


def test_capturar_falha_do_executor_reporta_ao_remetente_sem_fingir():
    class Explode:
        def disparar(self, url):
            raise RuntimeError("db fora do ar")
    ath, tg, _ = _montar(executor=Explode())
    ath.atender(100, "/capturar http://x")
    txt = _para(tg, 100)[0]
    assert "db fora do ar" in txt                  # honesto: não finge sucesso
    assert _para(tg, 999) == []


# --- /restart ---------------------------------------------------------------
def test_restart_reinicia_servico_conhecido():
    ath, tg, runner = _montar()
    ath.atender(100, "/restart worker")
    restarts = [c for c in runner.calls if "docker restart" in c and "worker" in c]
    assert len(restarts) == 1
    assert "worker" in _para(tg, 100)[0]


def test_restart_servico_desconhecido_nao_reinicia_nada():
    # DENTE: nome inexistente NÃO pode virar um `docker restart` cego.
    ath, tg, runner = _montar()
    ath.atender(100, "/restart fantasma")
    assert [c for c in runner.calls if "docker restart" in c] == []
    assert "encontrado" in _para(tg, 100)[0].lower()


def test_restart_sem_arg_mostra_uso():
    ath, tg, runner = _montar()
    ath.atender(100, "/restart")
    assert [c for c in runner.calls if "docker restart" in c] == []
    assert "uso" in _para(tg, 100)[0].lower()


# --- /prioridade ------------------------------------------------------------
def test_prioridade_sem_arg_mostra_a_fila():
    ath, tg, _ = _montar(prioridades=["urlA", "urlB"])
    ath.atender(100, "/prioridade")
    txt = _para(tg, 100)[0]
    assert "urlA" in txt and "urlB" in txt


def test_prioridade_com_link_move_pro_topo():
    fila = []
    ath, tg, _ = _montar(prioridades=fila)
    ath.atender(100, "/prioridade urlB")
    ath.atender(100, "/prioridade urlA")
    assert fila == ["urlA", "urlB"]                # último pedido fica no topo
    ath.atender(100, "/prioridade urlB")           # re-priorizar não duplica
    assert fila == ["urlB", "urlA"]


# --- desconhecido -----------------------------------------------------------
def test_comando_desconhecido_responde_sem_agir():
    ath, tg, runner = _montar()
    ath.atender(100, "/foobar")
    assert runner.calls == []                       # nenhuma ação disparada
    assert _para(tg, 100)                           # respondeu (ao remetente)
    assert _para(tg, 999) == []                     # sem broadcast


# --- SEGURANÇA: só o operador comanda a infra -------------------------------
def test_comando_de_chat_nao_autorizado_e_ignorado():
    # DENTE: um /restart vindo de um chat que NÃO é o operador não pode virar
    # `docker restart` — nem sequer uma resposta (anti-reflector: não confirma
    # que o bot existe). Sem esse gate, qualquer um que ache o bot mexe na infra.
    ath, tg, runner = _montar()                 # autorizados = {100}
    ath.atender(66666, "/restart worker")       # 66666 = estranho
    assert [c for c in runner.calls if "docker restart" in c] == []   # não agiu
    assert tg.enviadas == []                                          # nem respondeu


def test_comando_do_operador_autorizado_age_normalmente():
    # Contraprova do gate: o operador (100) continua comandando de verdade.
    ath, tg, runner = _montar()
    ath.atender(100, "/restart worker")
    assert [c for c in runner.calls if "docker restart" in c and "worker" in c]
    assert _para(tg, 100)


def test_autorizados_none_nega_tudo_fail_closed():
    # DENTE (fail-closed): sem allow-list configurada, NADA é atendido — nem
    # sequer o chat que seria o "operador" nos outros testes (100). O gate deixa
    # de ser opt-in: autorizados=None NEGA tudo, exigindo whitelist explícita.
    # (Antes deste fix, autorizados=None ATENDIA qualquer chat — inclusive um
    # estranho como 66666 — que é exatamente o comportamento que este teste
    # existia pra consagrar; foi reescrito porque o comportamento antigo era o
    # bug, não uma garantia a preservar.)
    ath, tg, runner = _montar(autorizados=None)
    ath.atender(66666, "/restart worker")
    ath.atender(100, "/restart worker")
    assert [c for c in runner.calls if "docker restart" in c] == []
    assert tg.enviadas == []


# --- HONESTIDADE: handler que quebra não vira silêncio ----------------------
def test_handler_que_quebra_responde_erro_ao_remetente_sem_silencio():
    # DENTE: se servicos() explode (docker sumiu), /status NÃO pode LEVANTAR nem
    # SILENCIAR — o operador tem que saber que engasgou (senão confia numa falha
    # invisível). O erro vai ao REMETENTE, não é broadcast ao operador (999).
    tg = FakeTG()
    def boom(cmd, timeout=None):
        raise RuntimeError("docker sumiu")
    acesso = Acesso(run_cmd=boom)
    voz = Voz(tg, [999])
    ath = Athena(tg, voz, acesso=acesso, autorizados={100})
    ath.atender(100, "/status")                 # não pode propagar exceção
    txt = _para(tg, 100)
    assert txt and "falhei" in txt[0].lower()   # respondeu o erro, honesto
    assert _para(tg, 999) == []                 # não vazou como broadcast


# --- NOTIFICAR: mensagem só sai quando há DECISÃO real ----------------------
def test_notificar_envia_quando_requer_decisao():
    ath, tg, _ = _montar()
    enviou = ath.notificar(Evento("sessao_morta", "http://c", "sessão morta",
                                  requer_decisao=True, pedido="preciso de RESEED"))
    assert enviou is True
    assert any("preciso de RESEED" in t for (_, t) in tg.enviadas)


def test_notificar_silencia_quando_nao_ha_decisao():
    ath, tg, _ = _montar()
    enviou = ath.notificar(Evento("progresso", "http://c", "50% capturado",
                                  requer_decisao=False))
    assert enviou is False
    assert tg.enviadas == []                        # ANTI-SPAM: nada sai


def test_notificar_descarta_flag_truthy_nao_booleana():
    # DENTE: se o gate usar `if requer_decisao:` em vez de `is True`, um valor
    # truthy-mas-não-True ("sim") vazaria spam. Tem que exigir True explícito.
    ath, tg, _ = _montar()
    enviou = ath.notificar(Evento("ruido", "x", "?", requer_decisao="sim"))
    assert enviou is False
    assert tg.enviadas == []


# --- loop de recepção: despacha e AVANÇA o offset ---------------------------
def test_rodar_processa_update_e_avanca_offset():
    # 1ª chamada entrega um /status (update_id=5); depois, vazio.
    tg_roteiro = [[Update(update_id=5, chat_id=100, texto="/status")]]
    ath, tg, _ = _montar(roteiro=tg_roteiro, captura_fn=lambda: "ok")
    ath.rodar(max_iters=2, sleep=lambda s: None)
    assert _para(tg, 100)                            # o /status foi atendido
    # DENTE: sem avançar o offset, o mesmo update seria reprocessado pra sempre.
    assert tg.offsets == [0, 6]                      # 6 = update_id(5) + 1


# --- PERSISTÊNCIA do offset: sobrevive a um restart do PROCESSO -------------
def test_rodar_persiste_offset_evita_reentrega_apos_restart():
    # DECISÃO DE ARQUITETURA: hoje rodar() sempre começa com offset=0 -> após um
    # restart do processo (deploy, crash, restart manual), o Telegram REENTREGA
    # até 24h de updates antigos, e a Athena reprocessaria /restart, /capturar
    # etc. já executados uma vez (replay). O fix: offset_load/offset_save são um
    # seam INJETÁVEL (callable load/save, sem acoplar a nenhum DB) — quem chama
    # rodar() injeta um store simples (aqui, um dict — em produção, um arquivo).
    store = {}

    def carregar():
        return store.get("offset", 0)

    def salvar(offset):
        store["offset"] = offset

    # "processo 1": recebe e processa o update 5, e PERSISTE o offset resultante.
    tg_roteiro = [[Update(update_id=5, chat_id=100, texto="/status")]]
    ath, tg, _ = _montar(roteiro=tg_roteiro, captura_fn=lambda: "ok")
    ath.rodar(max_iters=1, sleep=lambda s: None,
              offset_load=carregar, offset_save=salvar)
    assert tg.offsets == [0]          # 1º boot: nada persistido ainda -> começa do zero
    assert store["offset"] == 6       # processou o update 5 -> persistiu 6
    assert _para(tg, 100)             # de fato tratou o /status

    # "processo 2" = RESTART: nova instância da Athena (processo novo), mas o
    # MESMO store persistido (sobrevive ao restart, ao contrário da memória).
    ath2, tg2, _ = _montar(roteiro=[[]], captura_fn=lambda: "ok")
    ath2.rodar(max_iters=1, sleep=lambda s: None,
               offset_load=carregar, offset_save=salvar)
    # DENTE: sem a persistência, offset2 recomeçaria em 0 -> pediria ao Telegram
    # os updates a partir do zero de novo (replay de update_id=5 incluso). Com o
    # fix, o primeiro get_updates do processo novo já pede a partir do offset
    # PERSISTIDO (6) — o Telegram nem reentrega o update antigo.
    assert tg2.offsets == [6]


# --- offset ANCORADO em caminho ABSOLUTO (achado menor: relativo depende do cwd) ---
from maestro.athena import resolver_offset_path


def test_resolver_offset_path_relativo_ancora_no_base_dir():
    # DENTE do achado 'offset em caminho relativo': um relativo depende do cwd; um
    # restart de OUTRO diretório leria/gravaria outro arquivo e reprocessaria comandos.
    # O resolver ancora o relativo no base_dir estável (o dir do registro) -> ABSOLUTO.
    assert resolver_offset_path("athena_offset.txt", "/srv/maestro") == \
        "/srv/maestro/athena_offset.txt"
    # default (None/vazio) também ancora
    assert resolver_offset_path(None, "/srv/maestro") == "/srv/maestro/athena_offset.txt"
    assert resolver_offset_path("", "/srv/maestro") == "/srv/maestro/athena_offset.txt"


def test_resolver_offset_path_absoluto_e_respeitado():
    # Um caminho ABSOLUTO já é estável -> passa intacto (a escolha explícita do dono).
    assert resolver_offset_path("/data/off.txt", "/srv/maestro") == "/data/off.txt"


def test_resolver_offset_path_resultado_e_sempre_absoluto():
    # A GARANTIA central: qualquer entrada relativa vira absoluta (imune ao cwd).
    import os
    assert os.path.isabs(resolver_offset_path("x/off.txt", "/srv/maestro"))
    assert os.path.isabs(resolver_offset_path("off.txt", os.path.abspath(".")))
