"""RODADA 2, ITEM 1 — a classe do exit 4 sai da SAÍDA INTEIRA do motor, não das 40 linhas.

O ACHADO: `_classe_do_abort` lia `obito.stderr_tail`, que o vigia já cortou em 40 linhas
(`vigia._STDERR_LINHAS`), embora o executor copie 64 KB no reap (`captura._CAUDA_REAP_BYTES`).
O ruído de encerramento — o log do httpx das últimas gravações no Notion, o progresso do
yt-dlp, o call log do Playwright — empurra a frase do abort para fora; a regra cai em "parede
da plataforma" e um abort LOCAL/YouTube conta na série e bencha. Em produção: 26 de 195
autópsias exit-4 sem frase de classe nas 40 linhas; as de hotmart-principal de 23/07 02:08
(fim em httpx) e 04:28 (fim em yt-dlp) parecem locais.

AGORA: o vigia entrega a cauda BRUTA (últimos 64 KB) no `Obito.stderr_bruto` — só em memória,
fora do JSON — e a classe é decidida sobre ela. A autópsia em disco segue com 40 linhas.
"""
import glob
import json

from tests.test_antiban_exit4_classe_e_expiracao import (_ABORT_LOCAL, _ABORT_YOUTUBE_SUSPENSO,
                                                        _abortar)
from tests.test_antiban_relancamento_exit4 import (_ABORT_EXIT4, T0, VIRAL, _Daemon,
                                                   _sem_tracker_vivo)  # noqa: F401 (autouse)

# o fim real de 23/07 02:08 (hotmart-principal): as gravações no Notion depois do abort
_RUIDO_HTTPX = "".join(
    f'2026-07-23 02:08:{i % 60:02d},{i:03d} INFO httpx: HTTP Request: POST '
    f'https://api.notion.com/v1/pages "HTTP/1.1 200 OK"\n' for i in range(60))
# o fim real de 23/07 04:28: o progresso do yt-dlp
_RUIDO_YTDLP = "".join(f"[download] 100% of   {20 + i % 9}.03MiB\n                             \n\n"
                       for i in range(25))


def test_frase_local_empurrada_para_fora_das_40_linhas_continua_local(tmp_path):
    d = _Daemon(tmp_path)
    _abortar(d, _ABORT_LOCAL + _RUIDO_HTTPX, 3)
    assert not d.st.get("benched_exit4"), "o ruído do fim transformou o abort local em parede"
    assert not d.st.get("exit4_seguidas")


def test_frase_do_youtube_empurrada_para_fora_das_40_linhas_continua_youtube(tmp_path):
    d = _Daemon(tmp_path)
    _abortar(d, _ABORT_YOUTUBE_SUSPENSO + _RUIDO_YTDLP, 3)
    assert not d.st.get("benched_exit4") and not d.st.get("exit4_seguidas")


def test_linha_de_maquina_seguida_de_ruido_ainda_vence_a_prosa(tmp_path):
    d = _Daemon(tmp_path)
    _abortar(d, _ABORT_EXIT4 + "ABORT_DISJUNTOR tipo=local\n" + _RUIDO_HTTPX, 3)
    assert not d.st.get("benched_exit4")


def test_a_parede_com_ruido_continua_parede(tmp_path):
    d = _Daemon(tmp_path)
    _abortar(d, _ABORT_EXIT4 + _RUIDO_HTTPX, 3)
    assert d.st.get("benched_exit4") is True


def test_a_autopsia_em_disco_continua_com_40_linhas_e_sem_a_cauda_bruta(tmp_path):
    d = _Daemon(tmp_path)
    d.ciclo(T0, 100)
    d.ex.matar(VIRAL, exit_code=4, stderr=_ABORT_LOCAL + _RUIDO_HTTPX)
    d.ciclo(T0 + 120, 100)
    [arq] = glob.glob(str(tmp_path / "aut" / "*.json"))
    with open(arq, encoding="utf-8") as f:
        rec = json.load(f)
    assert len(rec["stderr_tail"].splitlines()) <= 40
    assert "stderr_bruto" not in rec, "a cauda de 64 KB foi parar em toda autópsia do disco"
