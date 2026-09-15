"""RELANÇAMENTO APÓS O CIRCUIT-BREAKER (exit 4) — trilha anti-ban aprovada pelo dono (15/09).

O INCIDENTE (13–14/09, `~/.athena-local/decisoes/2026-09-13.jsonl` e `2026-09-14.jsonl`): o
Cademí `cursos.codigoviral.com.br` foi disparado 17 vezes em ~14h. Todo run abortou no
disjuntor do motor ("5 erros nas últimas 5 aulas", exit 4) e o daemon o RELANÇOU ~1 min
depois da escalada (22:21→22:22, 22:58→22:59, 23:05→23:06) — inclusive depois da 4ª falha,
quando a escada já devia segurar 1 h. Só parou quando a sessão morreu. `alfaresearch` e
`luanacarolina` (Entrega Digital) fizeram o mesmo laço.

O MECANISMO — três defeitos que se somam:
  1. o run que aborta PRODUZIU aulas antes do disjuntor do motor; a passada vê o Notion subir
     e chama `registrar_sucesso`, que ZERA a escada 10min→1h→6h→24h que a autópsia acabou de
     armar (é por isso que nem a 4ª falha segurava);
  2. a autópsia roda ANTES da passada no MESMO ciclo: sem espera própria, o curso volta a ser
     disparado ali mesmo, ~1 min depois de morrer contra a parede;
  3. nada conta exit-4 em série: diferente do exit-5, o curso nunca é benchado.

Dublês com dentes: o executor de óbitos + os MÓDULOS REAIS (disjuntor/vigia/causa). Nenhum
tracker.db vivo (ATHENA_MOTOR_DIR aponta para um temporário) e nenhuma plataforma.
"""
import pytest

from maestro import athena_local, causa, disjuntor, vigia
from maestro.rotulo import casa_ancora_de_morte
from tests.test_athena_integracao import FakeExecutorObitos, FakeVoz, _curso, _prog

VIRAL = "https://cursos.codigoviral.com.br/"
IRMAO = "https://cursos.codigoviral.com.br/curso/outro"     # MESMA conta (tenant)
CONTA = "cademi-codigoviral"
T0 = 1_789_000_000.0

# A cauda REAL de um abort do disjuntor do motor (a frase é a de `run_course`; o GET é da
# autópsia 20260914T062315 do codigoviral). Classificada `escalar_humano` — conferido abaixo.
_ABORT_EXIT4 = (
    "  - → GET https://stream.smartplayer.io/66e33abe/66f1f353_0_en_192k.mp4\n"
    "  - ← 200 OK\n"
    "RUN ABORTADO com 7/300 aulas concluídas: 5 erros nas últimas 5 aulas. Algo está "
    "sistematicamente errado do lado da Hotmart — pode ser o anti-bot, a sessão, a rede, "
    "ou um detector nosso ruim. Continuar martelando é o caminho do banimento. Parando com "
    "ok=7 audio=0 falhou=5 de 300.\n")
_CAUDA_TOKEN = ("anthropic.AuthenticationError: Error code: 401 - {'type': 'error', "
                "'error': {'type': 'authentication_error', 'message': 'invalid x-api-key'}}")


@pytest.fixture(autouse=True)
def _sem_tracker_vivo(monkeypatch, tmp_path):
    # a classificação do exit 4 lê os erros do tracker em ATHENA_MOTOR_DIR — nunca o vivo
    monkeypatch.setenv("ATHENA_MOTOR_DIR", str(tmp_path / "motor-sem-tracker"))


class _Alertas:
    """Spy que guarda o GATE (`essencial`) e a CHAVE de dedup de cada alerta."""

    def __init__(self):
        self.mortes = []                                   # (plat, motivo, essencial, chave)
        self.sessoes = []

    def captura_morreu(self, plataforma, motivo, *, essencial=False, chave=None):
        self.mortes.append((plataforma, motivo, essencial, chave))

    def sessao_expirada(self, plataforma, **kw):
        self.sessoes.append(plataforma)

    def curso_concluido(self, *a, **kw):
        return None

    def maquina_sobrecarregada(self, *a, **kw):
        return None


class _Daemon:
    """UM daemon (estado/voo/executor na memória) com os módulos REAIS das 6 partes."""

    def __init__(self, tmp_path, *, estado=None):
        self.ex = FakeExecutorObitos({VIRAL: CONTA, IRMAO: CONTA})
        self.voz = FakeVoz()
        self.alertas = _Alertas()
        self.estado = {} if estado is None else estado
        self.voo = {}
        self._kw = dict(disjuntor=disjuntor, vigia=vigia, causa=causa, alertas=self.alertas,
                        lock_dir=str(tmp_path / "locks"), autopsia_dir=str(tmp_path / "aut"))

    def ciclo(self, agora, no_notion, *, cursos=(VIRAL,), total=300):
        lista = [_curso(u, CONTA, "cademi", total) for u in cursos]
        athena_local.ciclo_local(lista, self.ex, _prog({u: (no_notion, total) for u in cursos}),
                                 self.voz, self.voo, self.estado, agora=agora, **self._kw)

    @property
    def st(self):
        return self.estado[VIRAL]

    def disparos(self, curso=VIRAL):
        return self.ex.disparos.count(curso)


def test_a_cauda_do_fixture_e_um_exit4_de_causa_nomeada_para_humano():
    # guarda do dublê: se a cauda virasse reseed/token, os testes abaixo exercitariam outro ramo
    obito = type("O", (), {"curso": VIRAL, "conta": CONTA, "exit_code": 4,
                           "stderr_tail": _ABORT_EXIT4})()
    assert causa.classificar(obito, plataforma="cademi").acao == "escalar_humano"


# ==========================================================================
# 1) o AVANÇO no Notion de um run que abortou NÃO apaga a escada
# ==========================================================================
def test_avanco_no_notion_depois_de_exit4_nao_apaga_a_escada(tmp_path):
    d = _Daemon(tmp_path)
    d.ciclo(T0, 100)                                       # dispara
    d.ciclo(T0 + 120, 104)                                 # capturando: o Notion sobe
    d.ex.matar(VIRAL, exit_code=4, stderr=_ABORT_EXIT4)    # o disjuntor do motor abortou
    d.ciclo(T0 + 240, 107)                                 # autópsia + as aulas finais do run
    assert d.st.get("disj_falhas") == 1, "o avanço do run que abortou zerou a escada"

    d.ciclo(T0 + 900, 107)                                 # a espera venceu: relança
    assert d.disparos() == 2
    d.ciclo(T0 + 1020, 111)
    d.ex.matar(VIRAL, exit_code=4, stderr=_ABORT_EXIT4)
    d.ciclo(T0 + 1140, 115)
    assert d.st.get("disj_falhas") == 2, "a 2ª falha também foi apagada pelo avanço"


# ==========================================================================
# 2) um exit 4 NÃO é relançado no ciclo da autópsia — espera uma janela mínima
# ==========================================================================
def test_exit4_espera_a_janela_minima_antes_de_relancar(tmp_path):
    d = _Daemon(tmp_path)
    d.ciclo(T0, 100)
    d.ex.matar(VIRAL, exit_code=4, stderr=_ABORT_EXIT4)
    d.ciclo(T0 + 120, 100)                                 # ciclo da autópsia
    assert d.disparos() == 1, "relançado ~1 min depois de abortar contra a parede"
    d.ciclo(T0 + 420, 100)                                 # 5 min depois: ainda espera
    assert d.disparos() == 1
    d.ciclo(T0 + 720, 100)                                 # 10 min depois: never-stop
    assert d.disparos() == 2


def test_mesmo_com_a_espera_zerada_o_exit4_nao_relanca_no_ciclo_da_autopsia(tmp_path,
                                                                            monkeypatch):
    monkeypatch.setattr(athena_local, "_ESPERA_EXIT4_S", 0.0, raising=False)
    d = _Daemon(tmp_path)
    d.ciclo(T0, 100)
    d.ex.matar(VIRAL, exit_code=4, stderr=_ABORT_EXIT4)
    d.ciclo(T0 + 120, 100)
    assert d.disparos() == 1, "a env zerada devolveu o relançamento no mesmo ciclo"
    d.ciclo(T0 + 240, 100)                                 # o ciclo SEGUINTE pode
    assert d.disparos() == 2


# ==========================================================================
# 3) N exit-4 SEGUIDOS benchem o curso (como o exit-5) — o laço do codigoviral
# ==========================================================================
def test_tres_exit4_seguidos_com_notion_subindo_bencham_o_curso_e_a_conta_segue(tmp_path):
    d = _Daemon(tmp_path)
    t, n = T0, 100
    for _ in range(3):                                     # o padrão do incidente, 3 vezes
        d.ciclo(t, n)                                      # dispara (ou relança)
        d.ciclo(t + 120, n + 4)                            # capturou algumas aulas...
        d.ex.matar(VIRAL, exit_code=4, stderr=_ABORT_EXIT4)  # ...e abortou no disjuntor
        n += 7
        d.ciclo(t + 240, n)                                # autópsia
        t += 900                                           # além da espera mínima
    assert d.disparos() == 3
    assert d.st.get("benched_exit4") is True, "3 exit-4 seguidos e o curso não foi benchado"
    assert d.st.get("irredutivel") is True
    # o dono é chamado pelo gate ESSENCIAL, dedupado por curso, sem frase de morte
    bench = [m for m in d.alertas.mortes if m[3] == ("bench_exit4", VIRAL)]
    assert bench and bench[-1][2] is True, d.alertas.mortes
    assert "exit-4" in bench[-1][1] and not casa_ancora_de_morte(bench[-1][1]), bench
    assert d.alertas.sessoes == []                         # INVARIANTE: não é reseed

    # horas depois (a escada teria vencido), ainda dentro da janela do bench: NÃO relança.
    # (Reescrito na revisão de 15/09: afirmava "7 dias depois não relança" — o bench sem
    # prazo era parte do defeito; a expiração tem teste próprio em
    # test_antiban_exit4_classe_e_expiracao.py.)
    d.ciclo(t + 20 * 3600, n)
    assert d.disparos() == 3, "o curso benchado voltou a ser disparado"
    # o Notion do tenant sobe por OUTRA via (outro curso da mesma conta): não desbencha
    d.ciclo(t + 20 * 3600 + 120, n + 30, cursos=(VIRAL, IRMAO))
    assert d.disparos() == 3 and d.st.get("benched_exit4") is True
    assert d.disparos(IRMAO) == 1                          # e a CONTA segue nos demais cursos


def test_saida_limpa_zera_a_serie_de_exit4_e_devolve_o_recozimento(tmp_path):
    # never-stop: um run que termina LIMPO depois de dois exit-4 prova o curso são — a
    # série zera e a escada volta ao degrau zero (senão um curso bom acabaria benchado
    # por azar espalhado ao longo de semanas)
    d = _Daemon(tmp_path)
    d.ciclo(T0, 100)
    d.ex.matar(VIRAL, exit_code=4, stderr=_ABORT_EXIT4)
    d.ciclo(T0 + 120, 105)
    d.ciclo(T0 + 800, 105)                                 # relança
    d.ex.matar(VIRAL, exit_code=4, stderr=_ABORT_EXIT4)
    d.ciclo(T0 + 920, 110)
    d.ciclo(T0 + 1600, 110)                                # relança
    d.ciclo(T0 + 1720, 130)                                # este run PRODUZ (lido em voo)...
    d.ex.matar(VIRAL, exit_code=0, stderr="Stats: ok=21 audio=0 falhou=0 de 21")
    d.ciclo(T0 + 1840, 130)                                # ...e sai limpo, sem aula nova
                                                           # desde a última leitura
    assert d.disparos() == 4                               # segue capturando
    assert not d.st.get("exit4_seguidas")
    assert d.st.get("disj_falhas", 0) == 0, "a saída limpa que produziu não re-armou a escada"
    d.ex.matar(VIRAL, exit_code=4, stderr=_ABORT_EXIT4)    # um exit-4 isolado depois
    d.ciclo(T0 + 1960, 131)
    assert not d.st.get("benched_exit4")


def test_exit4_por_credencial_de_api_nao_conta_para_o_bench(tmp_path):
    # a chave recusada é da API downstream: benchar engoliria o alerta "troque o token" e
    # travaria o curso sem via de volta. A escada (sem avanço, nada a zerar) já espaça.
    d = _Daemon(tmp_path)
    t = T0
    for _ in range(3):
        d.ciclo(t, 100)
        d.ex.matar(VIRAL, exit_code=4, stderr=_CAUDA_TOKEN)
        d.ciclo(t + 120, 100)
        t += 4 * 3600
    assert not d.st.get("benched_exit4")
    assert any(m[3] == ("token", VIRAL) for m in d.alertas.mortes)


# ==========================================================================
# 4) a série e a espera atravessam o reinício do daemon; o bench (latch) não
# ==========================================================================
def test_serie_espera_e_prazo_do_bench_atravessam_o_reinicio(tmp_path):
    # Reescrito na revisão de 15/09: antes o bench NÃO atravessava o reinício (latch sem
    # destravamento além do boot) e cada reinício concedia uma tentativa. Com PRAZO
    # (`benched_exit4_ate`), persistir é seguro — ele se solta sozinho — e um reinício do
    # vigia externo não encurta mais o castigo. Os booleanos continuam fora da lista branca:
    # o latch é RECONSTRUÍDO do prazo.
    path = str(tmp_path / "estado_cursos.json")
    athena_local._gravar_estado_cursos(path, {VIRAL: {
        "disj_falhas": 3, "disj_bloqueado_ate": T0 + 600.0, "exit4_seguidas": 3,
        "exit4_ultimo": T0, "exit4_ate": T0 + 600.0, "benched_exit4_ate": T0 + 86400.0,
        "benched_exit4": True, "irredutivel": True}})
    st = athena_local._carregar_estado_cursos(path, agora=T0)[VIRAL]
    assert st == {"disj_falhas": 3, "disj_bloqueado_ate": T0 + 600.0, "exit4_seguidas": 3,
                  "exit4_ultimo": T0, "exit4_ate": T0 + 600.0,
                  "benched_exit4_ate": T0 + 86400.0}, st

    # encarnação nova: o bench vale até o prazo; vencido, UMA tentativa; outro abort de
    # plataforma rebencha NA HORA, em vez de conceder uma série nova de 3
    d = _Daemon(tmp_path, estado={VIRAL: st})
    d.ciclo(T0 + 3600, 120)
    assert d.disparos() == 0, "o reinício soltou o bench antes do prazo"
    d.ciclo(T0 + 86400 + 60, 120)
    assert d.disparos() == 1
    d.ex.matar(VIRAL, exit_code=4, stderr=_ABORT_EXIT4)
    d.ciclo(T0 + 86400 + 180, 126)
    assert d.st.get("benched_exit4") is True
