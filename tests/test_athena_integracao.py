"""DENTES DA INTEGRAÇÃO das 6 partes no loop doméstico (maestro.athena_local).

Cada teste injeta os MÓDULOS REAIS (controle/disjuntor/vigia/causa/batimento/alertas) no
`ciclo_local`/`rodar` e falha se a fiação regredir para os contratos ERRADOS da P1
(assinaturas idealizadas que não batiam com os módulos reais). São os 4 bugs críticos:

  P5  controle.filtrar_cursos(cursos, PATH)  — pausar tem de LER o controle.yaml no ciclo.
  P4  disjuntor.pode_tentar/registrar_falha(st, agora) — recozimento no gate.
  P3  vigia.autopsia(lock_dir, obitos)+causa.classificar(obito) — na morte de um curso.
  P2  batimento.talvez_bater(voz, resumo, agora, ultimo) + alertas typados (mudo LOGA).
"""
import asyncio
import logging

from maestro import athena_local, batimento, causa, controle, disjuntor, vigia
from maestro.adaptadores import captura

C1 = "https://hotmart.com/pt-br/x/products/111"
MK = "https://minha.memberkit.com.br/9"


class FakeVoz:
    def __init__(self):
        self.avisos = []
        self.escaladas = []

    def avisar_acao(self, acao):
        self.avisos.append(acao)

    def escalar(self, problema, pedido):
        self.escaladas.append((problema, pedido))

    def _enviar(self, texto):                              # p/ o batimento (Voz real)
        self.escaladas.append(("_enviar", texto))


class SpyAlertas:
    def __init__(self):
        self.mortes = []
        self.sessoes = []
        self.conclusoes = []

    def captura_morreu(self, plataforma, motivo, **kw):
        self.mortes.append((plataforma, motivo))

    def sessao_expirada(self, plataforma, **kw):
        self.sessoes.append(plataforma)

    def curso_concluido(self, plataforma, curso, n, **kw):
        self.conclusoes.append((plataforma, curso, n))


class FakeExecutorObitos:
    """Modela o LocalExecutor com a costura de ÓBITOS (drenar_obitos) que alimenta o vigia.
    `matar` simula a morte de um filho (exit_code / stderr), enfileirando o óbito por conta."""
    def __init__(self, conta_de):
        self._conta_de = conta_de
        self.ativos = set()
        self.disparos = []
        self._obitos = {}

    def curso_ativo(self, curso):
        return curso in self.ativos

    def disparar(self, curso):
        conta = self._conta_de[curso]
        if curso in self.ativos:
            return f"ja:{curso}"
        for c in self.ativos:
            if self._conta_de[c] == conta:
                raise captura.ContaOcupada(f"conta {conta} ocupada")
        self.ativos.add(curso)
        self.disparos.append(curso)
        return f"local_iniciada:{curso}"

    def matar(self, curso, *, exit_code=None, stderr=None):
        self.ativos.discard(curso)
        conta = self._conta_de[curso]
        self._obitos[conta] = {"conta": conta, "curso": curso, "exit_code": exit_code,
                               "stderr_tail": stderr, "pid": None}

    def drenar_obitos(self):
        out = dict(self._obitos)
        self._obitos = {}
        return out


def _curso(url, conta="a", plataforma="hotmart", total=0):
    return captura.CursoLocal(url=url, conta=conta, plataforma=plataforma,
                              total_esperado=total)


def _prog(mapa):
    return lambda curso: mapa.get(curso, (0, 0))


def _reais(tmp_path, alertas):
    """Kwargs que injetam os MÓDULOS REAIS das 6 partes no ciclo."""
    return dict(disjuntor=disjuntor, vigia=vigia, causa=causa, alertas=alertas,
                lock_dir=str(tmp_path / "locks"), autopsia_dir=str(tmp_path / "aut"))


# ==========================================================================
# P5 — CONTROLE: plataforma pausada em controle.yaml NÃO é RE-disparada
# (bug: P1 chamava filtrar_cursos(cursos) SEM o path -> pausar não fazia nada).
# ==========================================================================
def test_p5_plataforma_pausada_nao_dispara(tmp_path):
    cpath = str(tmp_path / "controle.yaml")
    controle.pausar_plataforma(cpath, "hotmart")           # pausa hotmart
    voz = FakeVoz()
    ex = FakeExecutorObitos({C1: "a", MK: "b"})
    estado, voo = {}, {}
    cursos = [_curso(C1, "a", "hotmart", 18), _curso(MK, "b", "memberkit", 5)]
    athena_local.ciclo_local(cursos, ex, _prog({C1: (0, 18), MK: (0, 5)}), voz, voo,
                             estado, agora=1000.0, controle=controle, controle_path=cpath)
    assert C1 not in ex.disparos                            # DENTES: pausado NÃO dispara
    assert MK in ex.disparos                                # não-pausado segue


def test_p5_reativar_volta_a_disparar(tmp_path):
    cpath = str(tmp_path / "controle.yaml")
    controle.pausar_plataforma(cpath, "hotmart")
    controle.ativar_plataforma(cpath, "hotmart")           # reativa
    voz = FakeVoz()
    ex = FakeExecutorObitos({C1: "a"})
    athena_local.ciclo_local([_curso(C1, "a", "hotmart", 18)], ex, _prog({C1: (0, 18)}),
                             voz, {}, {}, agora=1000.0, controle=controle, controle_path=cpath)
    assert C1 in ex.disparos


# ==========================================================================
# P3+P4 — MORTE por SESSÃO -> autópsia -> causa reseed -> IRREDUTÍVEL: não martela + alerta
# (bug: P1 chamava vigia.autopsia(curso, st) e causa.classificar(laudo)->'concluido'.)
# ==========================================================================
def test_p3p4_sessao_morta_vira_irredutivel_e_alerta(tmp_path):
    voz = FakeVoz()
    alr = SpyAlertas()
    ex = FakeExecutorObitos({C1: "a"})
    estado, voo = {}, {}
    cursos = [_curso(C1, "a", "hotmart", 18)]
    common = _reais(tmp_path, alr)
    athena_local.ciclo_local(cursos, ex, _prog({C1: (0, 18)}), voz, voo, estado,
                             agora=1000.0, **common)
    assert C1 in ex.disparos
    ex.matar(C1, exit_code=1,
             stderr="playwright._impl SessionExpiredError: please log in again")
    n = len(ex.disparos)
    athena_local.ciclo_local(cursos, ex, _prog({C1: (0, 18)}), voz, voo, estado,
                             agora=1100.0, **common)
    assert estado[C1].get("irredutivel") is True           # DENTES: sessão morta = irredutível
    assert len(ex.disparos) == n                            # NÃO re-dispara (não martela)
    assert alr.sessoes == ["hotmart"]                       # alerta typado de reseed


# ==========================================================================
# P3+P4 — MORTE por TOKEN (credencial de API) -> NÃO é irredutível: back off + alerta,
# e RE-TENTA sob o gate. A chave é uma API DOWNSTREAM (Groq/Anthropic), não a plataforma
# raspada: latch permanente não evita ban nenhum, só condena o curso a parar de vez (o
# bug do incidente Stoa, 1h28 latchado). DENTES: contra o antigo (escalar_token
# irredutível), o curso NUNCA voltaria a disparar.
# ==========================================================================
def test_p3p4_token_nao_e_irredutivel_backoff_e_realerta(tmp_path):
    voz = FakeVoz()
    alr = SpyAlertas()
    ex = FakeExecutorObitos({C1: "a"})
    estado, voo = {}, {}
    cursos = [_curso(C1, "a", "hotmart", 18)]
    common = _reais(tmp_path, alr)
    prog = _prog({C1: (0, 18)})
    # dispara + morre por credencial de API (401) — causa.classificar -> escalar_token.
    athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=1000.0, **common)
    n_antes = ex.disparos.count(C1)                        # 1 disparo inicial
    ex.matar(C1, exit_code=1, stderr="openai.AuthenticationError: HTTP 401 Unauthorized")
    # ciclo seguinte: autopsia -> escalar_token -> back off (NÃO irredutível) -> re-dispara.
    athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=1001.0, **common)

    assert not estado[C1].get("irredutivel")               # DENTES: token NÃO latcha de vez
    assert estado[C1].get("disj_falhas", 0) >= 1           # o disjuntor CONTABILIZOU a falha
    assert any("troque o token" in m for _, m in alr.mortes)  # alerta ao dono mantido
    # DENTES: no antigo (escalar_token irredutível) o curso ficava travado e NUNCA
    # re-disparava; agora o recozimento o reabre já no crédito livre.
    assert ex.disparos.count(C1) == n_antes + 1


# ==========================================================================
# P3+P4 — MORTE transitória (SIGKILL) -> causa relancar -> RECOZIMENTO do disjuntor:
# re-tenta enquanto no crédito; após o limiar arma o backoff (não dispara na janela) e
# RE-ARMA quando a janela expira. Prova que o disjuntor está fiado no gate.
# ==========================================================================
def test_p3p4_recozimento_arma_backoff_e_reabre(tmp_path):
    voz = FakeVoz()
    alr = SpyAlertas()
    ex = FakeExecutorObitos({C1: "a"})
    estado, voo = {}, {}
    cursos = [_curso(C1, "a", "hotmart", 18)]
    common = _reais(tmp_path, alr)
    prog = _prog({C1: (0, 18)})
    t = 1000.0
    # dispara + morre 3x. Cada morte é autopsiada no ciclo SEGUINTE (a autópsia abre o
    # ciclo), então após 3 disparos ainda há 1 morte pendente por contabilizar.
    for _ in range(3):
        athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=t, **common)
        ex.matar(C1, exit_code=-9)                          # SIGKILL -> causa 'relancar'
        t += 1
    assert ex.disparos.count(C1) == 3                       # re-disparou 3x (transitório)
    # 4º ciclo: autopsia a 3ª morte -> disj_falhas chega a 3 e ARMA a janela de backoff
    # (10 min) -> NÃO dispara, escala.
    athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=t, **common)
    assert estado[C1].get("disj_falhas") == 3               # o disjuntor CONTOU as falhas
    assert ex.disparos.count(C1) == 3                       # DENTES: backoff bloqueou
    assert any(p.tipo == "captura_local_esgotada" for p, _ in voz.escaladas)
    # muito depois (janela de 10 min expirou): RE-ARMA sozinho e volta a tentar (never-stop).
    athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=t + 3600.0, **common)
    assert ex.disparos.count(C1) == 4                       # recozimento reabriu


def test_p3p4_avanco_no_notion_rearma_o_recozimento(tmp_path):
    # AVANÇO real (no_notion sobe) -> registrar_sucesso zera o disjuntor (recozimento).
    voz = FakeVoz()
    alr = SpyAlertas()
    ex = FakeExecutorObitos({C1: "a"})
    estado, voo = {}, {}
    cursos = [_curso(C1, "a", "hotmart", 18)]
    common = _reais(tmp_path, alr)
    t = 1000.0
    for _ in range(3):
        athena_local.ciclo_local(cursos, ex, _prog({C1: (0, 18)}), voz, voo, estado,
                                 agora=t, **common)
        ex.matar(C1, exit_code=-9)
        t += 1
    assert estado[C1].get("disj_falhas", 0) >= 2           # o disjuntor acumulou falhas
    # agora o Notion AVANÇOU (0 -> 5): mesmo autopsiando a última morte, o sucesso
    # (registrar_sucesso) zera o disjuntor -> recozimento volta ao degrau zero.
    athena_local.ciclo_local(cursos, ex, _prog({C1: (5, 18)}), voz, voo, estado,
                             agora=t, **common)
    assert estado[C1].get("disj_falhas", 0) == 0           # DENTES: recozimento zerou


# ==========================================================================
# P2 — BATIMENTO periódico: mesmo MUDO, LOGA a linha que iria pro Telegram (dead-man's
# switch invertido); e não POSTA quando mudo.
# ==========================================================================
def test_p2_batimento_loga_mesmo_mudo(tmp_path, caplog, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "MUTED")      # Athena MUDA
    voz = FakeVoz()
    ex = FakeExecutorObitos({C1: "a"})

    async def _noop(_):
        return None

    with caplog.at_level(logging.INFO, logger="athena.batimento"):
        asyncio.run(athena_local.rodar(
            [_curso(C1, "a", "hotmart", 18)], ex, _prog({C1: (0, 18)}), voz, sleep=_noop,
            max_iters=1, intervalo_s=0.0, batimento=batimento, batimento_intervalo=0.0))
    linhas = [r.message for r in caplog.records if "viva:" in r.message]
    assert linhas                                          # DENTES: bateu (logou) mesmo mudo
    assert not any(t == "_enviar" for t, _ in voz.escaladas)  # mudo: NÃO postou


# ==========================================================================
# P2 — ALERTA de morte em FLAP: >= flap_min mortes na janela dispara captura_morreu
# ==========================================================================
def test_p2_flap_dispara_alerta_de_morte(tmp_path):
    voz = FakeVoz()
    alr = SpyAlertas()
    ex = FakeExecutorObitos({C1: "a"})
    estado, voo = {}, {}
    cursos = [_curso(C1, "a", "hotmart", 18)]
    common = _reais(tmp_path, alr)
    common["flap_min"] = 2                                  # 2 mortes na janela = flap
    prog = _prog({C1: (0, 18)})
    t = 1000.0
    for _ in range(3):
        athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=t, **common)
        ex.matar(C1, exit_code=-9)
        t += 1
    assert any("FLAP" in motivo for _, motivo in alr.mortes)  # DENTES: alertou o flap


# ==========================================================================
# BUG DO SUPERVISOR — curso JÁ CONCLUÍDO sai LIMPO (exit 0, total=0, Notion estável):
# NÃO é morte. Não conta flap, não vira "MORREU", não escala reseed, não avança o
# backoff, e ENTRA EM COOLDOWN (não re-spawna a cada ciclo). No código bugado cada
# saída-limpa virava falha -> re-dispara -> flap sobe -> alerta "FLAP: N mortes".
# ==========================================================================
def test_curso_concluido_saida_limpa_nao_e_morte_nem_flap_e_entra_em_cooldown(tmp_path):
    voz = FakeVoz()
    alr = SpyAlertas()
    ex = FakeExecutorObitos({C1: "a"})
    estado, voo = {}, {}
    cursos = [_curso(C1, "a", "hotmart", total=0)]         # total desconhecido (curso concluído)
    common = _reais(tmp_path, alr)
    common["flap_min"] = 2                                  # 2 "mortes" já dispararia FLAP
    prog = _prog({C1: (162, 0)})                           # 162 já no Notion, total=0
    t = 1000.0
    for _ in range(6):
        athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=t, **common)
        if ex.curso_ativo(C1):                             # só morre um filho que existe
            ex.matar(C1, exit_code=0, stderr="sessão: injetados 162 cookies")
        t += 100.0
    # DENTES: saída-limpa de curso concluído NUNCA vira alarme de morte/flap
    assert alr.mortes == []                                # nada de "MORREU"/"FLAP"
    assert alr.sessoes == []                               # NÃO escalou reseed (falso)
    assert not estado[C1].get("irredutivel")
    assert estado[C1].get("disj_falhas", 0) == 0           # backoff NÃO avançou (não é falha)
    assert ex.disparos.count(C1) == 1                      # DENTES: 1 disparo + cooldown (não martelou)
    assert estado[C1].get("cooldown_ate")                  # entrou em cooldown


def test_cooldown_de_saida_limpa_expira_e_reavalia(tmp_path):
    # O cooldown não é permanente: expirado, o curso volta a ser reavaliado (never-stop).
    voz = FakeVoz()
    alr = SpyAlertas()
    ex = FakeExecutorObitos({C1: "a"})
    estado, voo = {}, {}
    cursos = [_curso(C1, "a", "hotmart", total=0)]
    common = _reais(tmp_path, alr)
    prog = _prog({C1: (162, 0)})
    athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=1000.0, **common)
    ex.matar(C1, exit_code=0, stderr="sessão viva")
    athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=1100.0, **common)
    assert ex.disparos.count(C1) == 1                      # em cooldown: não re-dispara
    # MUITO depois (cooldown de horas expirou): reavalia e re-dispara (quieto, sem alarme).
    athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=1100.0 + 3 * 86400,
                             **common)
    assert ex.disparos.count(C1) == 2                      # DENTES: cooldown expirou -> reavaliou
    assert alr.mortes == []                                # e sem falso alarme


def test_morte_real_exit2_sessao_ainda_conta_e_escala_reseed(tmp_path):
    # DENTES anti-regressão do anti-ban: uma MORTE REAL (exit 2 = sessão morta pelo motor)
    # NÃO pode cair no ramo de saída-limpa — CONTINUA irredutível + escala reseed.
    voz = FakeVoz()
    alr = SpyAlertas()
    ex = FakeExecutorObitos({C1: "a"})
    estado, voo = {}, {}
    cursos = [_curso(C1, "a", "hotmart", total=0)]
    common = _reais(tmp_path, alr)
    athena_local.ciclo_local(cursos, ex, _prog({C1: (0, 0)}), voz, voo, estado,
                             agora=1000.0, **common)
    assert C1 in ex.disparos
    ex.matar(C1, exit_code=2)                              # exit 2 = SessionLostError (motor)
    n = len(ex.disparos)
    athena_local.ciclo_local(cursos, ex, _prog({C1: (0, 0)}), voz, voo, estado,
                             agora=1100.0, **common)
    assert estado[C1].get("irredutivel") is True          # sessão morta = irredutível
    assert len(ex.disparos) == n                           # não re-dispara (não martela)
    assert alr.sessoes == ["hotmart"]                      # escalou reseed (correto)
    assert not estado[C1].get("cooldown_ate")              # morte real NÃO vira cooldown de concluído


def test_morte_real_limpa_cooldown_herdado_de_saida_limpa(tmp_path):
    # DENTES anti-ban: se um curso estava em COOLDOWN (saída limpa) e depois MORRE de
    # verdade (sessão), o cooldown herdado NÃO pode mascarar a escalada — é limpo, e o
    # reseed escala normalmente.
    voz = FakeVoz()
    alr = SpyAlertas()
    ex = FakeExecutorObitos({C1: "a"})
    estado, voo = {}, {}
    cursos = [_curso(C1, "a", "hotmart", total=0)]
    common = _reais(tmp_path, alr)
    prog = _prog({C1: (162, 0)})
    # 1) dispara + sai LIMPO -> entra em cooldown
    athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=1000.0, **common)
    ex.matar(C1, exit_code=0, stderr="sessão viva")
    athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=1100.0, **common)
    assert estado[C1].get("cooldown_ate")                  # está em cooldown
    # 2) cooldown expira, re-dispara, e AGORA morre de verdade (sessão)
    t = 1100.0 + 3 * 86400
    athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=t, **common)
    assert ex.disparos.count(C1) == 2                      # reavaliou pós-cooldown
    ex.matar(C1, exit_code=2)                              # morte real (sessão)
    athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=t + 100, **common)
    assert estado[C1].get("irredutivel") is True           # DENTES: escalou como morte
    assert not estado[C1].get("cooldown_ate")              # cooldown herdado foi limpo
    assert alr.sessoes == ["hotmart"]


# ==========================================================================
# FIX 1 (observabilidade da autópsia) — a autópsia em disco tem de gravar o
# PORQUÊ: a Decisao da causa-raiz (acao/motivo/fonte) + plataforma. Sem isso,
# toda morte lida depois (sitrep/humano) via .get() vê None -> "causa
# desconhecida" mesmo quando a causa foi classificada na hora.
# ==========================================================================
import glob as _glob
import json as _json
import os as _os
import sqlite3 as _sqlite3


def _autopsias(tmp_path):
    recs = []
    for p in sorted(_glob.glob(str(tmp_path / "aut" / "*.json"))):
        with open(p) as f:
            recs.append(_json.load(f))
    return recs


def test_autopsia_grava_causa_motivo_fonte_e_plataforma(tmp_path):
    # RED-first: hoje o JSON da autópsia só tem o material bruto (exit_code/stderr);
    # a Decisao classificada NUNCA chega ao disco -> leitura vê None/"desconhecida".
    voz = FakeVoz()
    alr = SpyAlertas()
    ex = FakeExecutorObitos({C1: "a"})
    estado, voo = {}, {}
    cursos = [_curso(C1, "a", "hotmart", 18)]
    common = _reais(tmp_path, alr)
    athena_local.ciclo_local(cursos, ex, _prog({C1: (0, 18)}), voz, voo, estado,
                             agora=1000.0, **common)
    ex.matar(C1, exit_code=-9, stderr="Killed: 9")          # SIGKILL -> causa 'relancar'
    athena_local.ciclo_local(cursos, ex, _prog({C1: (0, 18)}), voz, voo, estado,
                             agora=1100.0, **common)
    recs = _autopsias(tmp_path)
    assert recs, "a morte tem de gravar autópsia"
    rec = recs[-1]
    # DENTES: a Decisao classificada está NO DISCO (leitura .get() nunca mais vê None)
    assert rec.get("causa") == "relancar", rec
    assert rec.get("acao") == "relancar", rec
    assert rec.get("fonte") == "deterministico", rec
    assert rec.get("plataforma") == "hotmart", rec
    assert rec.get("motivo") and rec.get("detalhe"), rec    # o PORQUÊ legível
    assert rec.get("stderr_tail") == "Killed: 9", rec       # material bruto NÃO regrediu
    assert rec.get("exit_code") == -9, rec


def test_autopsia_exit4_grava_erros_reais_do_tracker(tmp_path, monkeypatch):
    # RED-first (fim-a-fim do FIX 1): a PRÓXIMA morte exit-4 grava "causa: <erro real>"
    # — o motivo enriquecido com a coluna `error` do tracker (SQLite mode=ro) chega ao
    # JSON da autópsia via o default de produção (ATHENA_MOTOR_DIR).
    motor_dir = tmp_path / "motor"
    motor_dir.mkdir()
    con = _sqlite3.connect(str(motor_dir / "tracker.db"))
    con.execute("""CREATE TABLE lessons(
        course_id TEXT, order_idx INTEGER, url TEXT, status TEXT,
        notion_page_id TEXT, error TEXT, updated_at REAL)""")
    con.execute("INSERT INTO lessons VALUES ('111',1,'u','audio_erro',NULL,"
                "'TimeoutError REAL: chunk 3 estourou 120s',100.0)")
    con.commit()
    con.close()
    monkeypatch.setenv("ATHENA_MOTOR_DIR", str(motor_dir))
    voz = FakeVoz()
    alr = SpyAlertas()
    ex = FakeExecutorObitos({C1: "a"})                      # C1 tem /products/111
    estado, voo = {}, {}
    cursos = [_curso(C1, "a", "hotmart", 18)]
    common = _reais(tmp_path, alr)
    athena_local.ciclo_local(cursos, ex, _prog({C1: (0, 18)}), voz, voo, estado,
                             agora=1000.0, **common)
    ex.matar(C1, exit_code=4, stderr="RUN INTERROMPIDO POR EXCESSO DE FALHAS")
    athena_local.ciclo_local(cursos, ex, _prog({C1: (0, 18)}), voz, voo, estado,
                             agora=1100.0, **common)
    recs = _autopsias(tmp_path)
    assert recs
    rec = recs[-1]
    assert rec.get("causa") == "escalar_humano", rec
    # DENTES: o erro REAL do tracker está gravado na autópsia (não "desconhecida" cega)
    assert "TimeoutError REAL: chunk 3" in (rec.get("detalhe") or ""), rec


# ==========================================================================
# FIX 2 (bench do exit-5) — N mortes exit-5 IDÊNTICAS no MESMO curso (mesma URL;
# ex.: URL malformada …/products/X/agent que torna a sonda inconclusiva SEMPRE)
# NÃO podem virar `relancar` infinito (flap eterno). O curso é BENCHED
# (irredutível) + ALERTA com a URL. INVARIANTE ANTI-BAN: exit-5 = sonda
# INCONCLUSIVA, NÃO sessão morta — sem reseed, sem relogin; e o bench é
# por-CURSO: os demais cursos (mesmo da MESMA conta) seguem.
# ==========================================================================
_STDERR_EXIT5 = ("SONDA DE SESSÃO INCONCLUSIVA (transitório de rede/timeout): "
                 "probe /v1/navigation timeout")
C2 = "https://hotmart.com/pt-br/x/products/222"


def test_exit5_tres_mortes_identicas_bencham_o_curso_com_alerta_de_url(tmp_path):
    # RED-first: hoje 3 exit-5 idênticos -> relancar/backoff -> re-dispara para
    # sempre (flap). Com o fix: 3ª morte bencha o curso + alerta com a URL; a
    # conta NÃO para (outro curso da MESMA conta segue disparando); e NUNCA
    # escala reseed (sonda inconclusiva não é sessão morta).
    voz = FakeVoz()
    alr = SpyAlertas()
    ex = FakeExecutorObitos({C1: "a", C2: "a"})            # MESMA conta
    estado, voo = {}, {}
    cursos = [_curso(C1, "a", "hotmart", 18), _curso(C2, "a", "hotmart", 9)]
    common = _reais(tmp_path, alr)
    prog = _prog({C1: (0, 18), C2: (0, 9)})
    t = 1000.0
    for _ in range(3):                                      # 3 mortes exit-5 idênticas
        athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=t, **common)
        ex.matar(C1, exit_code=5, stderr=_STDERR_EXIT5)
        t += 1
    assert ex.disparos.count(C1) == 3                       # relançou nas 2 primeiras
    # 4º ciclo: autopsia a 3ª morte -> BENCH (não relança mais)
    athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=t, **common)
    assert estado[C1].get("benched_exit5") is True          # DENTES: curso benched
    assert estado[C1].get("irredutivel") is True            # disjuntor não re-tenta
    assert any("exit-5" in m and C1 in m for _, m in alr.mortes)  # ALERTA com a URL
    assert alr.sessoes == []                                # INVARIANTE: sem reseed
    # a CONTA não parou: o outro curso da mesma conta segue capturando
    assert C2 in ex.disparos
    # e o relancar infinito MORREU: muito depois (backoff expiraria), NÃO re-dispara
    athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado,
                             agora=t + 7 * 86400.0, **common)
    assert ex.disparos.count(C1) == 3                       # DENTES: benched de vez


def test_exit5_menos_que_o_limiar_segue_relancando(tmp_path):
    # 2 mortes exit-5 (< N=3): comportamento transitório preservado — relança sob
    # o backoff, sem bench, sem alerta de sessão.
    voz = FakeVoz()
    alr = SpyAlertas()
    ex = FakeExecutorObitos({C1: "a"})
    estado, voo = {}, {}
    cursos = [_curso(C1, "a", "hotmart", 18)]
    common = _reais(tmp_path, alr)
    prog = _prog({C1: (0, 18)})
    t = 1000.0
    for _ in range(2):
        athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=t, **common)
        ex.matar(C1, exit_code=5, stderr=_STDERR_EXIT5)
        t += 1
    athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=t, **common)
    assert not estado[C1].get("benched_exit5")              # 2 < 3: sem bench
    assert not estado[C1].get("irredutivel")
    assert ex.disparos.count(C1) >= 2                       # seguiu relançando


def test_exit5_avanco_no_notion_reseta_a_contagem_do_bench(tmp_path):
    # Um curso que MORRE exit-5 mas cujo Notion AVANÇA não é URL malformada — o
    # avanço reseta a contagem: a 3ª morte pós-avanço NÃO bencha.
    voz = FakeVoz()
    alr = SpyAlertas()
    ex = FakeExecutorObitos({C1: "a"})
    estado, voo = {}, {}
    cursos = [_curso(C1, "a", "hotmart", 18)]
    common = _reais(tmp_path, alr)
    t = 1000.0
    for _ in range(2):                                      # 2 mortes exit-5
        athena_local.ciclo_local(cursos, ex, _prog({C1: (0, 18)}), voz, voo, estado,
                                 agora=t, **common)
        ex.matar(C1, exit_code=5, stderr=_STDERR_EXIT5)
        t += 1
    # o Notion AVANÇOU (0 -> 5): a passada re-arma o recozimento E zera a contagem
    athena_local.ciclo_local(cursos, ex, _prog({C1: (5, 18)}), voz, voo, estado,
                             agora=t, **common)
    ex.matar(C1, exit_code=5, stderr=_STDERR_EXIT5)         # 3ª morte, pós-avanço
    athena_local.ciclo_local(cursos, ex, _prog({C1: (5, 18)}), voz, voo, estado,
                             agora=t + 1, **common)
    assert not estado[C1].get("benched_exit5")              # DENTES: avanço resetou
    assert not estado[C1].get("irredutivel")


def test_exit5_nao_mascara_sessao_realmente_morta(tmp_path):
    # INVARIANTE anti-ban: um exit-5 cujo stderr DECLARA a sessão morta segue a
    # escalada NORMAL de reseed (irredutível + sessao_expirada) — o bench jamais
    # engole uma sessão realmente morta.
    voz = FakeVoz()
    alr = SpyAlertas()
    ex = FakeExecutorObitos({C1: "a"})
    estado, voo = {}, {}
    cursos = [_curso(C1, "a", "hotmart", 18)]
    common = _reais(tmp_path, alr)
    athena_local.ciclo_local(cursos, ex, _prog({C1: (0, 18)}), voz, voo, estado,
                             agora=1000.0, **common)
    ex.matar(C1, exit_code=5, stderr="SESSÃO MORTA: refaça o login headed")
    athena_local.ciclo_local(cursos, ex, _prog({C1: (0, 18)}), voz, voo, estado,
                             agora=1100.0, **common)
    assert estado[C1].get("irredutivel") is True            # escalou como sessão
    assert alr.sessoes == ["hotmart"]                       # reseed alertado (correto)
    assert not estado[C1].get("benched_exit5")              # NÃO foi o bench


def test_exit5_serie_de_causa_token_nao_bencha_e_mantem_alerta_de_token(tmp_path):
    # REVIEW A1: o bench conta a série pela CAUSA CLASSIFICADA, não pelo exit-code cru.
    # 3 exit-5 cujo stderr denuncia CREDENCIAL (escalar_token) NÃO são a série
    # 'idêntica' da URL malformada: não bencham (bench seria um latch sem via de
    # desbench — curso nunca roda, Notion nunca avança), e o alerta de token de
    # CADA morte segue chegando ao dono (o bench não pode engolir o 3º).
    voz = FakeVoz()
    alr = SpyAlertas()
    ex = FakeExecutorObitos({C1: "a"})
    estado, voo = {}, {}
    cursos = [_curso(C1, "a", "hotmart", 18)]
    common = _reais(tmp_path, alr)
    prog = _prog({C1: (0, 18)})
    stderr_token = ("SONDA DE SESSÃO INCONCLUSIVA: HTTP 403 Forbidden na sonda "
                    "(invalid api key)")
    t = 1000.0
    mortes = 0
    for _ in range(12):                                     # saltos grandes: backoff expira
        athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=t, **common)
        if ex.curso_ativo(C1) and mortes < 3:
            ex.matar(C1, exit_code=5, stderr=stderr_token)
            mortes += 1
        t += 7 * 86400.0
        if mortes >= 3 and not ex.curso_ativo(C1):
            athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=t, **common)
            break
    assert mortes == 3
    assert not estado[C1].get("benched_exit5")              # DENTES A1: causa≠relancar não bencha
    assert not estado[C1].get("irredutivel")
    assert sum(1 for _, m in alr.mortes if "troque o token" in m) == 3  # nenhum engolido
    assert alr.sessoes == []                                # e jamais reseed


def test_obito_fantasma_sem_exit_code_nao_zera_a_serie_do_bench(tmp_path):
    # REVIEW A2 (interação nova): um óbito detectado só por PID morto (exit_code=None —
    # ex.: lock órfão de encarnação/conta antiga apontando o MESMO course_url) NÃO pode
    # quebrar a série de exit-5 confirmados — senão o fantasma re-zera o contador a cada
    # ciclo e o bench nunca dispara (o flap infinito que o fix veio matar continua).
    # Morte com exit_code REAL de outra causa continua quebrando a série (idênticas).
    from maestro.vigia import Obito

    class SpyDisj:
        def registrar_falha(self, st, agora):
            return None

    st = {"exit5_seguidas": 2}
    fantasma = Obito(conta="a", curso=C1, exit_code=None, stderr_tail="",
                     flaps_na_janela=1, ts="")
    athena_local._aplicar_decisao(
        C1, st, fantasma, None, disjuntor=SpyDisj(), alertas=SpyAlertas(),
        agora=1000.0, meta_por_curso=None, flap_min=99)
    assert st.get("exit5_seguidas") == 2                    # DENTES A2: fantasma NÃO zera
    real = Obito(conta="a", curso=C1, exit_code=-9, stderr_tail="Killed: 9",
                 flaps_na_janela=1, ts="")
    athena_local._aplicar_decisao(
        C1, st, real, None, disjuntor=SpyDisj(), alertas=SpyAlertas(),
        agora=1001.0, meta_por_curso=None, flap_min=99)
    assert "exit5_seguidas" not in st                       # morte confirmada ≠ exit-5: zera


# ==========================================================================
# GATE DE ESSENCIALIDADE no CANAL Telegram (fiação real): só o que exige AÇÃO
# HUMANA pinga o dono; auto-tratado (flap/relancar/bench) vira SÓ-LOG. Estes
# dentes usam o Alertas REAL com dublê de TelegramClient — provam o fio inteiro
# ciclo_local -> _aplicar_decisao -> Alertas -> Telegram.
# ==========================================================================
class _TGSpy:
    def __init__(self):
        self.msgs = []

    def send_message(self, chat, texto):
        self.msgs.append((chat, texto))


def _alertas_reais(tg):
    from maestro.alertas import Alertas
    return Alertas(tg, [1])


def test_flap_relancar_nao_pinga_telegram_mas_fica_no_log(tmp_path, caplog):
    # RUÍDO nº1 de hoje: morte transitória (SIGKILL -> relancar) em flap dispara
    # "MORREU" no Telegram a cada ciclo — mas o sistema re-tenta SOZINHO sob o
    # disjuntor. Essencial = NADA a fazer pelo dono => Telegram em silêncio; o
    # log continua registrando o flap (observabilidade intacta).
    voz = FakeVoz()
    tg = _TGSpy()
    ex = FakeExecutorObitos({C1: "a"})
    estado, voo = {}, {}
    cursos = [_curso(C1, "a", "hotmart", 18)]
    common = _reais(tmp_path, _alertas_reais(tg))
    common["flap_min"] = 2
    t = 1000.0
    with caplog.at_level(logging.WARNING, logger="athena.alertas"):
        for _ in range(3):
            athena_local.ciclo_local(cursos, ex, _prog({C1: (0, 18)}), voz, voo,
                                     estado, agora=t, **common)
            ex.matar(C1, exit_code=-9)                     # SIGKILL -> relancar
            t += 1
    assert tg.msgs == []                                   # DENTES: zero Telegram
    assert any("FLAP" in r.message for r in caplog.records)  # mas o log viu o flap


def test_sessao_morta_SEMPRE_pinga_telegram(tmp_path):
    # INVARIANTE: reseed é o único evento que SÓ o humano destrava — o gate de
    # essencialidade JAMAIS pode engoli-lo.
    voz = FakeVoz()
    tg = _TGSpy()
    ex = FakeExecutorObitos({C1: "a"})
    estado, voo = {}, {}
    cursos = [_curso(C1, "a", "hotmart", 18)]
    common = _reais(tmp_path, _alertas_reais(tg))
    athena_local.ciclo_local(cursos, ex, _prog({C1: (0, 18)}), voz, voo, estado,
                             agora=1000.0, **common)
    ex.matar(C1, exit_code=5, stderr="SESSÃO MORTA: refaça o login headed")
    athena_local.ciclo_local(cursos, ex, _prog({C1: (0, 18)}), voz, voo, estado,
                             agora=1100.0, **common)
    assert any("expirou" in texto for _, texto in tg.msgs)  # DENTES: reseed alertado


def test_escalar_humano_dedupado_1_envio_por_curso_na_janela(tmp_path):
    # Exit 4 (circuit-breaker, causa desconhecida) exige humano -> ALERTA, mas
    # DEDUPADO: 2 mortes do MESMO curso na mesma janela = 1 ping (não metralha).
    voz = FakeVoz()
    tg = _TGSpy()
    ex = FakeExecutorObitos({C1: "a"})
    estado, voo = {}, {}
    cursos = [_curso(C1, "a", "hotmart", 18)]
    common = _reais(tmp_path, _alertas_reais(tg))
    common["flap_min"] = 99                                 # isola o ramo escalar_humano
    t = 1000.0
    for _ in range(3):
        athena_local.ciclo_local(cursos, ex, _prog({C1: (0, 18)}), voz, voo,
                                 estado, agora=t, **common)
        ex.matar(C1, exit_code=4)                          # circuit-breaker -> humano
        t += 1
    mortes = [texto for _, texto in tg.msgs if "MORREU" in texto]
    assert len(mortes) == 1                                # DENTES: dedup na janela
