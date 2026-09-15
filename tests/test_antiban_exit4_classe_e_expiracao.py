"""EXIT 4 — SÓ A PAREDE DA PLATAFORMA BENCHA, E O BENCH EXPIRA (revisão independente de e1c5da9, 15/09).

O ACHADO BLOQUEANTE da revisão: e1c5da9 benchava QUALQUER exit 4 (menos o de credencial). Mas o
motor sai 4 também quando NÃO é a plataforma do curso:
  - o disjuntor LOCAL (Claude/Notion/Groq falhando: "É a Claude ou o Notion falhando — NÃO é a
    Hotmart", motor/orchestrator.py);
  - o YouTube suspenso/bloqueado no passe (Memberkit/Greenn/--youtube: "NÃO é a plataforma do
    curso e NÃO é a conta") e a rede dos vídeos declarados indisponíveis pelo YouTube.
Cenário: a cota diária da Groq acaba, ou a Anthropic cai 2–3 h -> todo curso aborta 3 vezes ->
TODOS benchados, em silêncio (o bench marcava `esgotado_avisado` e calava a única escalada que a
Voz ainda mandava), sem recuperação automática. Antes de e1c5da9: backoff e recuperação.

AGORA:
  - a CLASSE do abort sai da cauda que a autópsia já tem: a linha de máquina do motor
    `ABORT_DISJUNTOR tipo=local|plataforma|youtube` quando existir; senão a prosa do motor (a
    ocorrência MAIS RECENTE); sem marcador, PAREDE DA PLATAFORMA (conservador para a conta);
  - local/YouTube: a espera mínima e a escada normal continuam, mas NÃO contam série nem benchem;
  - a série da plataforma DECAI (abort anterior mais velho que ATHENA_BENCH_EXIT4_EXPIRA_S);
  - o bench EXPIRA na mesma janela: UMA tentativa, e o próximo abort de plataforma rebencha;
  - o bench não cala a escalada `captura_local_esgotada` da Voz;
  - os textos dizem a classe real; envs inválidas caem no padrão sem derrubar o import.
"""
import os
import subprocess
import sys

import pytest

from maestro import athena_local, causa
from maestro.rotulo import casa_ancora_de_morte
from tests.test_antiban_relancamento_exit4 import (_ABORT_EXIT4, IRMAO, T0, VIRAL, _Daemon,
                                                   _sem_tracker_vivo)  # noqa: F401 (autouse)

H = 3600.0
DIA = 86400.0
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# As caudas REAIS de cada classe (frases de motor/orchestrator.py e dos CLIs em 2cf9d04).
_ABORT_LOCAL = (
    "RUN ABORTADO com 3/300 aulas concluídas: 5 falhas locais nas últimas 5 aulas. É a Claude "
    "ou o Notion falhando — NÃO é a Hotmart, e NÃO é risco de banimento. Parei para não queimar "
    "créditos de API à toa. Parando com ok=3 audio=0 falhou=5 de 300.\n"
    "\n⛔ RUN INTERROMPIDO POR EXCESSO DE FALHAS: 5 falhas locais nas últimas 5 aulas. É a "
    "Claude ou o Notion falhando — NÃO é a Hotmart, e NÃO é risco de banimento. Parei para não "
    "queimar créditos de API à toa. Parando com ok=3 audio=0 falhou=5 de 300.\n"
    "\nO QUE FAZER — nesta ordem:\n"
    "1. OLHE OS ERROS no tracker antes de qualquer coisa. A causa pode ser\n"
    "   banal (um detector nosso ruim, a Claude/Notion fora do ar) ou séria\n"
    "   (o anti-bot da Hotmart acordou). O motor NÃO sabe qual é.\n")
_ABORT_YOUTUBE_SUSPENSO = (
    "\n⛔ YOUTUBE SUSPENSO NO PASSE — BLOQUEIO DO YOUTUBE: o YouTube ficou fora do alcance deste "
    "IP neste passe (verificação anti-bot, limite de taxa, IP barrado, recusa 403 / formato "
    "indisponível). NÃO é a plataforma do curso e NÃO é a conta: é o caminho até o YouTube (IP "
    "residencial/cliente/rede). O YouTube ficou SUSPENSO no resto do passe — elas seguem na "
    "fila, retentáveis. Motivo: limite de taxa do YouTube. Passe Memberkit: ok=4 audio=0 "
    "falhou=2 de 6.\n"
    "Nenhuma aula do YouTube foi marcada como concluída nem terminal por isso.\n")
_ABORT_YOUTUBE_TERMINAIS = (
    "\n⛔ PASSE PAROU: VÍDEO(S) DO YOUTUBE VIERAM COMO INDISPONÍVEIS: o passe completou com ZERO "
    "capturas e 3 de 3 aula(s) do passe foram declaradas indisponível/privada/restrita pelo "
    "YouTube. As demais, se houver, seguem retentáveis.\n")
# a cauda do codigoviral de 14/09 06:23: só o call log do Playwright, nenhuma frase de abort
_CAUDA_SEM_MARCADOR = (
    "  - → GET https://stream.smartplayer.io/66e33abe/66f1f353_0_en_192k.mp4\n"
    "  - ← 200 OK\n    - server: CloudFront\n    - x-cache: Hit from cloudfront\n    - vary: Origin\n")
_ROTULO = {"plataforma": "parede da plataforma do curso",
           "local": "provedor local (Claude/Notion/Groq)",
           "youtube": "bloqueio do YouTube"}


def _abortar(d, stderr, vezes, *, t=T0, n=100, passo=4 * H):
    """`vezes` runs que abortam no disjuntor do motor, espaçados além da espera e da escada."""
    for _ in range(vezes):
        d.ciclo(t, n)
        d.ex.matar(VIRAL, exit_code=4, stderr=stderr)
        d.ciclo(t + 120, n)                                # ciclo da autópsia
        t += passo
    return t


@pytest.mark.parametrize("cauda", [_ABORT_LOCAL, _ABORT_YOUTUBE_SUSPENSO,
                                   _ABORT_YOUTUBE_TERMINAIS, _CAUDA_SEM_MARCADOR])
def test_as_caudas_do_fixture_sao_exit4_para_humano_e_nao_reseed_nem_token(cauda):
    obito = type("O", (), {"curso": VIRAL, "conta": "c", "exit_code": 4, "stderr_tail": cauda})()
    assert causa.classificar(obito, plataforma="cademi").acao == "escalar_humano"


# ==========================================================================
# 1) só a PAREDE DA PLATAFORMA conta série e bencha
# ==========================================================================
def test_tres_exit4_de_provedor_local_esperam_mas_nao_bencham(tmp_path):
    d = _Daemon(tmp_path)
    d.ciclo(T0, 100)
    d.ex.matar(VIRAL, exit_code=4, stderr=_ABORT_LOCAL)
    d.ciclo(T0 + 120, 100)
    assert d.disparos() == 1, "a espera mínima deixou de valer para o exit 4 local"
    _abortar(d, _ABORT_LOCAL, 2, t=T0 + 4 * H)             # 2º e 3º aborts locais
    assert d.disparos() == 3
    assert not d.st.get("benched_exit4"), "a cota da Groq/queda da Anthropic benchou o curso"
    assert not d.st.get("irredutivel") and not d.st.get("exit4_seguidas")
    d.ciclo(T0 + 12 * H, 100)                              # never-stop: a escada vence, relança
    assert d.disparos() == 4


@pytest.mark.parametrize("cauda", [_ABORT_YOUTUBE_SUSPENSO, _ABORT_YOUTUBE_TERMINAIS],
                         ids=["suspenso", "terminais"])
def test_exit4_do_youtube_nao_conta_para_o_bench(tmp_path, cauda):
    d = _Daemon(tmp_path)
    _abortar(d, cauda, 3)
    assert d.disparos() == 3
    assert not d.st.get("benched_exit4") and not d.st.get("exit4_seguidas")


@pytest.mark.parametrize("cauda,bencha", [
    (_ABORT_EXIT4 + "ABORT_DISJUNTOR tipo=local\n", False),
    (_ABORT_LOCAL + "ABORT_DISJUNTOR tipo=plataforma\n", True),
    (_ABORT_LOCAL + "ABORT_DISJUNTOR tipo=youtube\nlinha solta do encerramento\n", False),
], ids=["maquina-local-vence-prosa-de-parede", "maquina-plataforma-vence-prosa-local",
        "maquina-youtube"])
def test_a_linha_de_maquina_do_motor_vence_a_prosa(tmp_path, cauda, bencha):
    d = _Daemon(tmp_path)
    _abortar(d, cauda, 3)
    assert bool(d.st.get("benched_exit4")) is bencha


@pytest.mark.parametrize("cauda,bencha", [
    ("RUN ABORTADO com 2/80 aulas concluídas: 5 falhas locais nas últimas 5 aulas. É a Claude "
     "ou o Notion falhando — NÃO é a Cademí, e NÃO é risco de banimento. Parei para não queimar "
     "créditos de API à toa. Parando com ok=2 audio=0 falhou=5 de 80.\n", False),
    ("RUN ABORTADO com 7/80 aulas concluídas: 5 erros nas últimas 5 aulas. Algo está "
     "sistematicamente errado do lado da Cademí — pode ser o anti-bot, a sessão, a rede, ou um "
     "detector nosso ruim. Continuar martelando é o caminho do banimento.\n", True),
], ids=["local-2cf9d04", "parede-2cf9d04"])
def test_a_classe_nao_depende_do_nome_da_plataforma_no_texto(tmp_path, cauda, bencha):
    # motor 2cf9d04 (no ar desde 15/09 10:34): "do lado da Hotmart"/"NÃO é a Hotmart" viraram
    # o nome REAL da plataforma. Nenhuma frase casada pelo daemon cita a Hotmart.
    d = _Daemon(tmp_path)
    _abortar(d, cauda, 3)
    assert bool(d.st.get("benched_exit4")) is bencha


def test_cauda_sem_marcador_de_abort_conta_como_parede_da_plataforma(tmp_path):
    # conservador para a conta: 20 das 192 autópsias exit-4 de produção não trazem frase
    # nenhuma do abort nas 40 linhas (o call log do Playwright empurra); elas benchem
    d = _Daemon(tmp_path)
    _abortar(d, _CAUDA_SEM_MARCADOR, 3)
    assert d.st.get("benched_exit4") is True


# ==========================================================================
# 2) o bench EXPIRA e a série DECAI
# ==========================================================================
def test_o_bench_expira_concede_UMA_tentativa_e_o_proximo_abort_rebencha(tmp_path):
    d = _Daemon(tmp_path)
    _abortar(d, _ABORT_EXIT4, 3)
    tb = T0 + 8 * H + 120                                  # ciclo que benchou
    assert d.st.get("benched_exit4") is True
    d.ciclo(tb + 23 * H, 100)
    assert d.disparos() == 3                               # dentro da janela: benchado
    d.ciclo(tb + DIA + 60, 100)
    assert d.disparos() == 4, "o bench não expirou (curso parado para sempre até um reinício)"
    assert not d.st.get("benched_exit4")
    d.ex.matar(VIRAL, exit_code=4, stderr=_ABORT_EXIT4)    # a tentativa bate na parede de novo
    d.ciclo(tb + DIA + 180, 100)
    assert d.st.get("benched_exit4") is True, "a tentativa pós-expiração ganhou uma série nova"
    d.ciclo(tb + DIA + 2 * H, 100)
    assert d.disparos() == 4


def test_a_serie_decai_quando_o_abort_anterior_e_mais_velho_que_a_janela(tmp_path):
    d = _Daemon(tmp_path)
    _abortar(d, _ABORT_EXIT4, 2)                           # T0 e T0+4h
    d.ciclo(T0 + 30 * H, 100)                              # > 24 h depois do 2º abort
    assert d.disparos() == 3
    d.ex.matar(VIRAL, exit_code=4, stderr=_ABORT_EXIT4)
    d.ciclo(T0 + 30 * H + 120, 100)
    assert not d.st.get("benched_exit4"), "aborts de dias atrás completaram a série"
    assert d.st.get("exit4_seguidas") == 1


def test_a_expiracao_do_bench_nunca_solta_um_latch_de_reseed(tmp_path):
    d = _Daemon(tmp_path)
    _abortar(d, _ABORT_EXIT4, 3)
    tb = T0 + 8 * H + 120
    d.ex.matar(VIRAL, exit_code=2, stderr="SessionDeadError: sessão expirada — faça o login")
    d.ciclo(tb + H, 100)
    assert d.alertas.sessoes                               # o latch agora é de reseed
    d.ciclo(tb + 3 * DIA, 100)
    assert d.disparos() == 3, "a expiração do bench soltou a sessão morta"


# ==========================================================================
# 3) o bench não fica mais calado que antes; os textos dizem a classe
# ==========================================================================
def test_o_bench_nao_cala_a_escalada_da_voz(tmp_path):
    d = _Daemon(tmp_path)
    _abortar(d, _ABORT_EXIT4, 3)
    d.ciclo(T0 + 8 * H + 120 + 700, 100)                   # passada depois da espera também
    esgotadas = [pedido for p, pedido in d.voz.escaladas
                 if getattr(p, "tipo", None) == "captura_local_esgotada" and VIRAL in pedido]
    assert esgotadas, "o bench calou a única escalada que chegava ao Telegram"


@pytest.mark.parametrize("cauda,classe", [(_ABORT_LOCAL, "local"),
                                          (_ABORT_YOUTUBE_SUSPENSO, "youtube"),
                                          (_ABORT_EXIT4, "plataforma")])
def test_os_textos_nomeiam_a_classe_real(tmp_path, cauda, classe):
    d = _Daemon(tmp_path)
    _abortar(d, cauda, 3)
    textos = [m[1] for m in d.alertas.mortes if m[3] in (("humano", VIRAL),
                                                       ("bench_exit4", VIRAL))]
    assert textos
    for texto in textos:
        assert _ROTULO[classe] in texto, (classe, texto)
        for outra, rotulo in _ROTULO.items():
            if outra != classe:
                assert rotulo not in texto, (classe, texto)
    # o texto do BENCH é do daemon (o do humano carrega o motivo da causa.py, que já diz
    # "NÃO é sessão morta" e casa a âncora CRUA — pré-existente, fora deste conserto)
    bench = [m[1] for m in d.alertas.mortes if m[3] == ("bench_exit4", VIRAL)]
    assert bool(bench) is (classe == "plataforma")
    for texto in bench:
        assert not casa_ancora_de_morte(texto), texto
    for rotulo in _ROTULO.values():
        assert not casa_ancora_de_morte(rotulo)


# ==========================================================================
# 4) env inválida cai no padrão, sem derrubar o import do daemon
# ==========================================================================
@pytest.mark.parametrize("espera,minimo,expira,esperado", [
    ("dez minutos", "três", "um dia", ["600.0", "3", "86400.0"]),
    ("inf", "0", "10", ["600.0", "1", "3600.0"]),
    ("nan", "-4", "inf", ["600.0", "1", "86400.0"]),
])
def test_env_invalida_do_exit4_cai_no_padrao_sem_derrubar_o_import(espera, minimo, expira,
                                                                  esperado):
    env = dict(os.environ, ATHENA_ESPERA_EXIT4_S=espera, ATHENA_BENCH_EXIT4_MIN=minimo,
               ATHENA_BENCH_EXIT4_EXPIRA_S=expira)
    r = subprocess.run(
        [sys.executable, "-c", "from maestro import athena_local as a; "
         "print(a._ESPERA_EXIT4_S, a._BENCH_EXIT4_MIN, a._BENCH_EXIT4_EXPIRA_S)"],
        cwd=RAIZ, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-1200:]
    assert r.stdout.split() == esperado, (r.stdout, r.stderr[-600:])
    assert "ATHENA_" in r.stderr                           # e avisa no log
