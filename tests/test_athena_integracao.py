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

    def captura_morreu(self, plataforma, motivo):
        self.mortes.append((plataforma, motivo))

    def sessao_expirada(self, plataforma):
        self.sessoes.append(plataforma)

    def curso_concluido(self, plataforma, curso, n):
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
