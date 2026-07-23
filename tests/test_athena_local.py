"""ENTRYPOINT DOMÉSTICO (`maestro.athena_local`): o loop no Mac que REUSA o owner
`orquestrar_captura`, só TROCANDO o executor (fila -> local).

Dublês COM DENTES: FakeExecutor modela o CONTRATO real do LocalExecutor — idempotência
por curso, `curso_ativo`, e o ANTI-BAN 1-por-conta que LEVANTA `ContaOcupada`. FakeVoz
registra avisos/escaladas. A contagem-verdade do Notion é um dict controlável. Cada
inviolável (anti-dup por completude, anti-ban, disjuntor, plataforma-nova, fail-closed)
tem um teste que FALHA se o comportamento for removido."""
import asyncio

import pytest

from maestro import athena_local
from maestro.adaptadores import captura

C1 = "https://hotmart.com/pt-br/x/products/111"
C2 = "https://hotmart.com/pt-br/y/products/222"
KIWIFY = "https://dooma.kiwify.com.br/curso-z"
PLATS = frozenset({"hotmart.com"})


class FakeVoz:
    def __init__(self):
        self.avisos = []
        self.escaladas = []

    def avisar_acao(self, acao):
        self.avisos.append(acao)

    def escalar(self, problema, pedido):
        self.escaladas.append((problema, pedido))


class FakeExecutor:
    """Modela o LocalExecutor: serializa por conta (anti-ban) e é idempotente por curso.
    `conta_de` mapeia curso->conta. `ativos` é o conjunto de cursos rodando agora."""
    def __init__(self, conta_de, *, boom=()):
        self._conta_de = conta_de
        self._boom = set(boom)                             # cursos cujo disparar LEVANTA
        self.ativos = set()
        self.disparos = []

    def curso_ativo(self, curso):
        return curso in self.ativos

    def disparar(self, curso):
        if curso in self._boom:
            raise RuntimeError("disparo local estourou")
        if curso in self.ativos:                           # idempotente por curso
            return f"ja:{curso}"
        conta = self._conta_de[curso]
        for c in self.ativos:                              # ANTI-BAN 1-por-conta
            if self._conta_de[c] == conta:
                raise captura.ContaOcupada(f"conta {conta} ocupada por {c}")
        self.ativos.add(curso)
        self.disparos.append(curso)
        return f"local_iniciada:{curso}"

    def terminar(self, curso):
        self.ativos.discard(curso)


def _curso(url, conta="a", plataforma="hotmart", total=0):
    return captura.CursoLocal(url=url, conta=conta, plataforma=plataforma,
                              total_esperado=total)


def _prog(mapa):
    """progresso_fn dublê: curso -> (no_notion, total). Ausente => (0, 0)."""
    return lambda curso: mapa.get(curso, (0, 0))


# ==========================================================================
# Fluxo básico: dispara UM curso incompleto pelo EXECUTOR LOCAL
# ==========================================================================
def test_dispara_curso_incompleto_pelo_executor_local():
    voz = FakeVoz()
    ex = FakeExecutor({C1: "a"})
    estado, voo = {}, {}
    res = athena_local.ciclo_local([_curso(C1, total=18)], ex, _prog({C1: (0, 18)}),
                                   voz, voo, estado, agora=1000.0)
    assert ex.disparos == [C1]                             # a captura foi disparada LOCAL
    assert estado[C1]["fase"] == captura.FASE_CAPTURANDO
    assert res[0].concluido is False and C1 in voo         # em vigília (incompleto)


def test_curso_ativo_nao_e_redisparado_no_ciclo_seguinte():
    voz = FakeVoz()
    ex = FakeExecutor({C1: "a"})
    estado, voo = {}, {}
    athena_local.ciclo_local([_curso(C1, total=18)], ex, _prog({C1: (0, 18)}), voz, voo,
                             estado, agora=1000.0)
    athena_local.ciclo_local([_curso(C1, total=18)], ex, _prog({C1: (5, 18)}), voz, voo,
                             estado, agora=1100.0)          # C1 ainda ativo
    assert ex.disparos == [C1]                             # DENTES: não re-disparou (quieto)


# ==========================================================================
# ANTI-DUP POR COMPLETUDE: curso já COMPLETO no Notion não é (re)capturado
# ==========================================================================
def test_curso_ja_completo_no_notion_nao_e_capturado():
    voz = FakeVoz()
    ex = FakeExecutor({C1: "a"})
    estado, voo = {}, {}
    res = athena_local.ciclo_local([_curso(C1, total=18)], ex, _prog({C1: (18, 18)}),
                                   voz, voo, estado, agora=1000.0)
    assert ex.disparos == []                               # DENTES: NÃO re-captura o pronto
    assert estado[C1]["fase"] == captura.FASE_CONCLUIDO
    assert res[0].concluido is True


def test_parcial_no_notion_ainda_captura():
    # DENTES anti-falso-pronto: presença (10/18) NÃO é completude -> RETOMA a captura.
    voz = FakeVoz()
    ex = FakeExecutor({C1: "a"})
    athena_local.ciclo_local([_curso(C1, total=18)], ex, _prog({C1: (10, 18)}),
                             voz, voo := {}, {}, agora=1000.0)
    assert ex.disparos == [C1]


def test_total_desconhecido_nunca_conclui_mas_captura():
    # total=0 (denominador desconhecido) NUNCA prova completude (fail-closed): não
    # conclui, mas também não impede a captura.
    voz = FakeVoz()
    ex = FakeExecutor({C1: "a"})
    estado, voo = {}, {}
    res = athena_local.ciclo_local([_curso(C1, total=0)], ex, _prog({C1: (5, 0)}),
                                   voz, voo, estado, agora=1000.0)
    assert ex.disparos == [C1]
    assert res[0].concluido is False
    assert estado[C1]["fase"] != captura.FASE_CONCLUIDO


# ==========================================================================
# ANTI-BAN EMERGENTE: 2 cursos na MESMA conta -> só 1 dispara; o outro aguarda
# ==========================================================================
def test_dois_cursos_mesma_conta_apenas_um_dispara_por_ciclo():
    voz = FakeVoz()
    ex = FakeExecutor({C1: "conta-A", C2: "conta-A"})
    estado, voo = {}, {}
    athena_local.ciclo_local(
        [_curso(C1, "conta-A", total=18), _curso(C2, "conta-A", total=18)],
        ex, _prog({C1: (0, 18), C2: (0, 18)}), voz, voo, estado, agora=1000.0)
    assert ex.disparos == [C1]                             # DENTES: só C1; C2 aguardou a vez
    # C2 aguardando NÃO conta como falha nem escala nem consome tentativa
    assert estado[C2].get("tentativas", 0) == 0
    assert voz.escaladas == []


def test_segundo_curso_dispara_quando_a_conta_libera():
    voz = FakeVoz()
    ex = FakeExecutor({C1: "conta-A", C2: "conta-A"})
    estado, voo = {}, {}
    cursos = [_curso(C1, "conta-A", total=18), _curso(C2, "conta-A", total=18)]
    athena_local.ciclo_local(cursos, ex, _prog({C1: (0, 18), C2: (0, 18)}), voz, voo,
                             estado, agora=1000.0)
    ex.terminar(C1)                                        # C1 acabou (mas incompleto)
    # C1 volta a poder disparar; mas o ciclo processa C1 primeiro e re-toma a conta.
    # Para provar que C2 dispara quando a conta está livre, marcamos C1 completo agora:
    athena_local.ciclo_local(cursos, ex, _prog({C1: (18, 18), C2: (0, 18)}), voz, voo,
                             estado, agora=1100.0)
    assert C2 in ex.disparos                               # a conta liberou -> C2 disparou


def test_contas_diferentes_disparam_no_mesmo_ciclo():
    voz = FakeVoz()
    ex = FakeExecutor({C1: "conta-A", C2: "conta-B"})
    estado, voo = {}, {}
    athena_local.ciclo_local(
        [_curso(C1, "conta-A", total=18), _curso(C2, "conta-B", total=18)],
        ex, _prog({C1: (0, 18), C2: (0, 18)}), voz, voo, estado, agora=1000.0)
    assert set(ex.disparos) == {C1, C2}                    # contas distintas: paralelo


# ==========================================================================
# DISJUNTOR: teto de tentativas -> para de disparar e escala UMA vez
# ==========================================================================
def test_disjuntor_para_de_disparar_apos_teto_de_tentativas():
    voz = FakeVoz()
    ex = FakeExecutor({C1: "a"})
    estado, voo = {}, {}
    cursos = [_curso(C1, total=18)]
    prog = _prog({C1: (0, 18)})
    # 3 tentativas: a cada ciclo o processo "morre" antes do próximo (incompleto)
    for t in range(3):
        athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=1000.0 + t)
        ex.terminar(C1)                                    # processo caiu sem completar
    assert estado[C1]["tentativas"] == 3
    n_disparos = len(ex.disparos)
    # 4º ciclo: teto atingido -> DISJUNTOR, não dispara mais e escala uma vez
    athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=2000.0)
    assert len(ex.disparos) == n_disparos                  # DENTES: NÃO martelou
    esgotadas = [p for p, _ in voz.escaladas if p.tipo == "captura_local_esgotada"]
    assert len(esgotadas) == 1
    # 5º ciclo: latch -> NÃO re-escala
    athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=2100.0)
    esgotadas = [p for p, _ in voz.escaladas if p.tipo == "captura_local_esgotada"]
    assert len(esgotadas) == 1


# ==========================================================================
# GATE DE PLATAFORMA-NOVA: curso sem adaptador é PULADO (escala latch), não capturado
# ==========================================================================
def test_plataforma_nova_e_pulada_nao_capturada():
    voz = FakeVoz()
    ex = FakeExecutor({KIWIFY: "k"})
    estado, voo = {}, {}
    athena_local.ciclo_local([_curso(KIWIFY, "k", plataforma="kiwify", total=10)], ex,
                             _prog({KIWIFY: (0, 10)}), voz, voo, estado, agora=1000.0,
                             plataformas_suportadas=PLATS)
    assert ex.disparos == []                               # DENTES: NÃO capturou plataforma nova
    tipos = [p.tipo for p, _ in voz.escaladas]
    assert "plataforma_nova" in tipos


# ==========================================================================
# FAIL-CLOSED: Notion ilegível -> escala, NÃO dispara às cegas
# ==========================================================================
def test_notion_ilegivel_nao_dispara_as_cegas():
    voz = FakeVoz()
    ex = FakeExecutor({C1: "a"})
    estado, voo = {}, {}

    def prog_boom(curso):
        raise RuntimeError("notion fora do ar")

    athena_local.ciclo_local([_curso(C1, total=18)], ex, prog_boom, voz, voo, estado,
                             agora=1000.0)
    assert ex.disparos == []                               # DENTES: não enfileira às cegas
    tipos = [p.tipo for p, _ in voz.escaladas]
    assert "progresso_notion_inacessivel" in tipos


def test_falha_de_disparo_escala_honesto():
    voz = FakeVoz()
    ex = FakeExecutor({C1: "a"}, boom=(C1,))
    estado, voo = {}, {}
    athena_local.ciclo_local([_curso(C1, total=18)], ex, _prog({C1: (0, 18)}), voz, voo,
                             estado, agora=1000.0)
    tipos = [p.tipo for p, _ in voz.escaladas]
    assert "captura_local_disparo_falhou" in tipos


# ==========================================================================
# CONCLUSÃO PROVADA PELO OWNER: o Notion alcança o total em ciclo posterior
# ==========================================================================
def test_conclui_quando_o_notion_alcanca_o_total():
    voz = FakeVoz()
    ex = FakeExecutor({C1: "a"})
    estado, voo = {}, {}
    cursos = [_curso(C1, total=18)]
    athena_local.ciclo_local(cursos, ex, _prog({C1: (0, 18)}), voz, voo, estado,
                             agora=1000.0)                  # dispara, entra em vigília
    assert C1 in voo
    res = athena_local.ciclo_local(cursos, ex, _prog({C1: (18, 18)}), voz, voo, estado,
                                   agora=1100.0)            # Notion prova 18/18
    assert res[0].concluido is True
    assert C1 not in voo                                    # saiu da vigília


# ==========================================================================
# Loop assíncrono: reinjeta estado/voo e respeita max_iters
# ==========================================================================
def test_rodar_itera_e_persiste_estado_cross_ciclo():
    voz = FakeVoz()
    ex = FakeExecutor({C1: "a"})
    prog = _prog({C1: (0, 18)})

    async def _noop_sleep(_):
        return None

    n = asyncio.run(athena_local.rodar([_curso(C1, total=18)], ex, prog, voz,
                                       sleep=_noop_sleep, max_iters=3, intervalo_s=0.0))
    assert n == 3
    # DENTES cross-ciclo: apesar de 3 ciclos, o curso foi disparado UMA vez (estado
    # persistiu: ciclos 2-3 viram C1 ativo e ficaram quietos).
    assert ex.disparos == [C1]


# ==========================================================================
# ACHADO MÉDIO: um ciclo que ESTOURA NÃO vira no-op silencioso — é ESCALADO.
# (antes: `except Exception: pass` engolia falha sistemática em silêncio.)
# ==========================================================================
async def _noop_sleep(_):
    return None


def test_rodar_escala_quando_o_ciclo_estoura(monkeypatch):
    voz = FakeVoz()
    ex = FakeExecutor({C1: "a"})

    def boom(*a, **k):
        raise RuntimeError("dependência doméstica quebrada")

    monkeypatch.setattr(athena_local, "ciclo_local", boom)
    asyncio.run(athena_local.rodar([_curso(C1, total=18)], ex, _prog({C1: (0, 18)}), voz,
                                   sleep=_noop_sleep, max_iters=3, intervalo_s=0.0))
    tipos = [p.tipo for p, _ in voz.escaladas]
    # DENTES: a falha do ciclo SURFA (não é engolida). Reverter para `except: pass`
    # deixaria `escaladas` vazio e este assert quebraria.
    assert "ciclo_local_estourou" in tipos
    # latch por assinatura: 3 ciclos com o MESMO erro escalam UMA vez (não inunda).
    assert tipos.count("ciclo_local_estourou") == 1


def test_rodar_rearma_o_latch_e_reescala_novo_episodio(monkeypatch):
    voz = FakeVoz()
    ex = FakeExecutor({C1: "a"})
    chamadas = {"n": 0}
    real = athena_local.ciclo_local

    def intermitente(*a, **k):
        chamadas["n"] += 1
        if chamadas["n"] in (1, 3):                        # falha, OK, falha
            raise RuntimeError("intermitente")
        return real(*a, **k)

    monkeypatch.setattr(athena_local, "ciclo_local", intermitente)
    asyncio.run(athena_local.rodar([_curso(C1, total=18)], ex, _prog({C1: (0, 18)}), voz,
                                   sleep=_noop_sleep, max_iters=3, intervalo_s=0.0))
    tipos = [p.tipo for p, _ in voz.escaladas]
    # um ciclo bem-sucedido no meio RE-ARMA o latch -> o 2º episódio re-escala honesto.
    assert tipos.count("ciclo_local_estourou") == 2


def test_rodar_nao_morre_se_a_voz_falhar_ao_escalar(monkeypatch):
    # a escalada é best-effort: se a própria voz estourar, o loop NÃO pode morrer.
    class VozRuim(FakeVoz):
        def escalar(self, problema, pedido):
            raise RuntimeError("telegram fora")

    voz = VozRuim()
    ex = FakeExecutor({C1: "a"})

    def boom(*a, **k):
        raise RuntimeError("ciclo estourou")

    monkeypatch.setattr(athena_local, "ciclo_local", boom)
    n = asyncio.run(athena_local.rodar([_curso(C1, total=18)], ex, _prog({C1: (0, 18)}),
                                       voz, sleep=_noop_sleep, max_iters=2, intervalo_s=0.0))
    assert n == 2                                          # completou os ciclos, não morreu


# ==========================================================================
# carregar_cursos: lê o YAML doméstico; conta é obrigatória
# ==========================================================================
def test_carregar_cursos_le_yaml(tmp_path):
    p = tmp_path / "cursos.yaml"
    p.write_text(
        "- url: https://hotmart.com/x/products/1\n"
        "  conta: rodrigo\n"
        "  total_esperado: 20\n"
        "- url: https://minha.memberkit.com.br/9\n"
        "  conta: ana\n"
        "  plataforma: memberkit\n")
    cursos = athena_local.carregar_cursos(str(p))
    assert cursos[0].conta == "rodrigo" and cursos[0].total_esperado == 20
    assert cursos[0].plataforma == "hotmart"               # default
    assert cursos[1].plataforma == "memberkit"


def test_carregar_cursos_conta_obrigatoria(tmp_path):
    p = tmp_path / "cursos.yaml"
    p.write_text("- url: https://hotmart.com/x/products/1\n")  # sem conta
    with pytest.raises(KeyError):
        athena_local.carregar_cursos(str(p))


# ==========================================================================
# progresso_local_fn / contar_no_notion_local: contagem-verdade do Notion LOCAL
# ==========================================================================
def test_contar_no_notion_local_parseia_a_sentinela():
    calls = []

    def fake_run(cmd, *, cwd):
        calls.append({"cmd": cmd, "cwd": cwd})
        return f"algum log\n{captura.PROGRESSO_SENTINELA} 12\n"

    n = athena_local.contar_no_notion_local("/py", "/dir", C1, run=fake_run)
    assert n == 12
    call = calls[0]
    assert call["cmd"][0] == "/py" and call["cmd"][1] == "-c"
    assert call["cwd"] == "/dir"                           # cwd do motor (carrega .env)
    assert C1.rstrip("/") + "/" in call["cmd"][-1]         # prefixo do curso vai como argv


def test_contar_no_notion_local_sem_sentinela_levanta():
    # DENTES fail-closed: silêncio NÃO vira 0 (subestimar re-capturaria) -> LEVANTA.
    def fake_run(cmd, *, cwd):
        return "erro qualquer, sem sentinela"

    with pytest.raises(RuntimeError):
        athena_local.contar_no_notion_local("/py", "/dir", C1, run=fake_run)


def test_progresso_local_fn_combina_numerador_e_total():
    def fake_run(cmd, *, cwd):
        return f"{captura.PROGRESSO_SENTINELA} 7\n"

    fn = athena_local.progresso_local_fn("/py", "/dir", {C1: 18}, run=fake_run)
    assert fn(C1) == (7, 18)


# ==========================================================================
# HIGIENE (2) — COOLDOWN-DE-CONCLUÍDO: curso SEM pendência capturável no tracker
# (só restam terminais: no_notion/sem_conteudo/falhou) NÃO é re-selecionado NEM
# escala exit-4 a cada ciclo. Falso-positivo real: invistodireito 533/547 no Notion
# (14 terminais: 13 sem_conteudo + 1 falhou) — nunca "completo por Notion" (533<547),
# mas ZERO trabalho capturável. Entra em cooldown longo.
# INVARIANTE: curso com pendência REAL (pendente/in-flight = throttle/parede) AINDA
# dispara e AINDA escala — done != bloqueado.
# ==========================================================================
def _pend(valor):
    """pendencia_fn dublê: curso -> nº de aulas capturáveis pendentes (None = desconhecido)."""
    return lambda curso: valor


def test_sem_pendencia_capturavel_nao_dispara_nem_escala():
    # invistodireito: Notion 533/547 (parcial), mas tracker sem pendência capturável.
    voz = FakeVoz()
    ex = FakeExecutor({C1: "a"})
    estado, voo = {}, {}
    athena_local.ciclo_local([_curso(C1, total=547)], ex, _prog({C1: (533, 547)}), voz,
                             voo, estado, agora=1000.0, pendencia_fn=_pend(0))
    assert ex.disparos == []                               # DENTE: NÃO re-dispara o done
    assert estado[C1].get("cooldown_ate") is not None      # entrou em cooldown longo
    assert voz.escaladas == []                             # DENTE: NÃO escala exit-4


def test_sem_pendencia_fica_quieto_dentro_do_cooldown():
    voz = FakeVoz()
    ex = FakeExecutor({C1: "a"})
    estado, voo = {}, {}
    cursos = [_curso(C1, total=547)]
    athena_local.ciclo_local(cursos, ex, _prog({C1: (533, 547)}), voz, voo, estado,
                             agora=1000.0, pendencia_fn=_pend(0))
    athena_local.ciclo_local(cursos, ex, _prog({C1: (533, 547)}), voz, voo, estado,
                             agora=1100.0, pendencia_fn=_pend(0))   # dentro da janela
    assert ex.disparos == []                               # segue quieto
    assert voz.escaladas == []


def test_pendencia_real_ainda_dispara():
    # INVARIANTE: há trabalho capturável (pendente/parede) -> dispara normalmente.
    voz = FakeVoz()
    ex = FakeExecutor({C1: "a"})
    estado, voo = {}, {}
    athena_local.ciclo_local([_curso(C1, total=547)], ex, _prog({C1: (533, 547)}), voz,
                             voo, estado, agora=1000.0, pendencia_fn=_pend(14))
    assert ex.disparos == [C1]                             # DENTE: pendência real -> captura


def test_pendencia_real_ainda_escala_no_teto():
    # INVARIANTE: disjuntor no teto + pendência REAL -> AINDA escala (done != bloqueado).
    voz = FakeVoz()
    ex = FakeExecutor({C1: "a"})
    estado, voo = {}, {}
    cursos = [_curso(C1, total=547)]
    prog = _prog({C1: (533, 547)})
    for t in range(3):
        athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=1000.0 + t,
                                 pendencia_fn=_pend(14))
        ex.terminar(C1)
    athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=2000.0,
                             pendencia_fn=_pend(14))
    esgotadas = [p for p, _ in voz.escaladas if p.tipo == "captura_local_esgotada"]
    assert len(esgotadas) == 1                             # DENTE: pendência real AINDA escala


def test_pendencia_desconhecida_fail_open_dispara():
    # tracker ilegível / curso não semeado -> None -> comportamento antigo (dispara).
    voz = FakeVoz()
    ex = FakeExecutor({C1: "a"})
    estado, voo = {}, {}
    athena_local.ciclo_local([_curso(C1, total=547)], ex, _prog({C1: (533, 547)}), voz,
                             voo, estado, agora=1000.0, pendencia_fn=_pend(None))
    assert ex.disparos == [C1]                             # fail-open: NÃO trava a captura


def test_sem_pendencia_com_avanco_no_notion_sai_do_cooldown():
    # Se o Notion AVANÇA (apareceu trabalho novo) o cooldown some e a captura retoma.
    voz = FakeVoz()
    ex = FakeExecutor({C1: "a"})
    estado, voo = {}, {}
    cursos = [_curso(C1, total=547)]
    athena_local.ciclo_local(cursos, ex, _prog({C1: (533, 547)}), voz, voo, estado,
                             agora=1000.0, pendencia_fn=_pend(0))
    assert ex.disparos == []
    # próximo ciclo: Notion subiu (534) E há pendência -> avanço limpa o cooldown, captura.
    athena_local.ciclo_local(cursos, ex, _prog({C1: (534, 547)}), voz, voo, estado,
                             agora=1100.0, pendencia_fn=_pend(5))
    assert ex.disparos == [C1]


# ==========================================================================
# HIGIENE (1) — REAPER no BOOT da lane: `rodar` chama o reaper UMA vez ANTES do
# primeiro ciclo (limpa órfãos async de uma encarnação anterior antes de capturar).
# ==========================================================================
def test_rodar_chama_reaper_no_boot_antes_do_primeiro_ciclo(monkeypatch):
    voz = FakeVoz()
    ex = FakeExecutor({C1: "a"})
    ordem = []
    orig_ciclo = athena_local.ciclo_local

    def espia_ciclo(*a, **k):
        ordem.append("ciclo")
        return orig_ciclo(*a, **k)

    monkeypatch.setattr(athena_local, "ciclo_local", espia_ciclo)
    asyncio.run(athena_local.rodar(
        [_curso(C1, total=18)], ex, _prog({C1: (0, 18)}), voz,
        sleep=_noop_sleep, max_iters=1, intervalo_s=0.0,
        reaper_fn=lambda: ordem.append("reaper")))
    assert ordem and ordem[0] == "reaper"                  # DENTE: reaper ANTES do 1º ciclo
    assert "ciclo" in ordem
