"""Batimento (heartbeat) da Athena: prova de VIDA periódica no Telegram.

O batimento é a ÚNICA mensagem proativa-e-periódica do sistema: alertas nascem
de PROBLEMAS (Sentinela/Voz), o batimento nasce do RELÓGIO. Invariantes
travados aqui:
  1. só bate quando `agora - ultimo >= intervalo` (senão devolve `ultimo` intacto);
  2. quando bate, o texto começa com "viva:" e carrega o resumo real;
  3. MUDO (TELEGRAM_BOT_TOKEN=MUTED, ou ausente) => NENHUM envio e NENHUM crash;
  4. best-effort: nem o resumo nem o envio podem levantar/derrubar o loop;
  5. um batimento que não pôde ser enviado ainda AVANÇA o relógio — não vira
     tempestade de retry a cada ciclo (o próximo intervalo tenta de novo).
"""
import maestro.batimento as batimento


class _Voz:
    """Dublê da Voz REAL (maestro/voz.py), que expõe o primitivo `_enviar`.
    Registra tudo que foi enviado para o teste inspecionar."""

    def __init__(self):
        self.enviados = []

    def _enviar(self, texto):
        self.enviados.append(texto)


def _token_vivo(monkeypatch):
    # token de aparência real (NÃO o MUTED) — nenhum I/O acontece nos testes,
    # o dublê da Voz só registra.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:fake-token-de-teste-abcdefghij")


# --- gatilho de relógio ---------------------------------------------------

def test_nao_bate_antes_do_intervalo(monkeypatch):
    _token_vivo(monkeypatch)
    v = _Voz()
    novo = batimento.talvez_bater(v, lambda: "x", agora=1000.0, ultimo=1000.0, intervalo=1800)
    assert novo == 1000.0        # relógio NÃO avança
    assert v.enviados == []      # nenhum envio


def test_nao_bate_um_segundo_antes(monkeypatch):
    _token_vivo(monkeypatch)
    v = _Voz()
    novo = batimento.talvez_bater(v, lambda: "x", agora=1000.0 + 1799, ultimo=1000.0, intervalo=1800)
    assert novo == 1000.0
    assert v.enviados == []


def test_bate_no_intervalo_avanca_relogio_e_envia(monkeypatch):
    _token_vivo(monkeypatch)
    v = _Voz()
    novo = batimento.talvez_bater(
        v, lambda: "2 capturas ativas, 487/500 no Notion, uptime 5h",
        agora=3000.0, ultimo=1000.0, intervalo=1800)
    assert novo == 3000.0
    assert v.enviados == ["viva: 2 capturas ativas, 487/500 no Notion, uptime 5h"]


def test_texto_comeca_com_viva(monkeypatch):
    _token_vivo(monkeypatch)
    v = _Voz()
    batimento.talvez_bater(v, lambda: "algo", agora=5000.0, ultimo=0.0, intervalo=1800)
    assert v.enviados[0].startswith("viva:")


# --- MUDO: regressão anti-envio -------------------------------------------

def test_mudo_nao_envia_e_nao_crash(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "MUTED")
    v = _Voz()
    novo = batimento.talvez_bater(v, lambda: "algo", agora=5000.0, ultimo=0.0, intervalo=1800)
    assert v.enviados == []      # NENHUM envio quando mudo
    assert novo == 5000.0        # relógio avança (não vira tempestade)


def test_token_ausente_conta_como_mudo(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    v = _Voz()
    novo = batimento.talvez_bater(v, lambda: "algo", agora=5000.0, ultimo=0.0, intervalo=1800)
    assert v.enviados == []
    assert novo == 5000.0


def test_token_vazio_conta_como_mudo(monkeypatch):
    # fail-safe: token vazio/espaço (ex.: .env com a linha em branco) => mudo,
    # sem depender do launch.sh converter "" em MUTED.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "   ")
    v = _Voz()
    novo = batimento.talvez_bater(v, lambda: "algo", agora=5000.0, ultimo=0.0, intervalo=1800)
    assert v.enviados == []
    assert novo == 5000.0


# --- best-effort: nunca levanta -------------------------------------------

def test_envio_que_levanta_nao_propaga(monkeypatch):
    _token_vivo(monkeypatch)

    class _VozRuim:
        def _enviar(self, texto):
            raise RuntimeError("telegram caiu")

    novo = batimento.talvez_bater(_VozRuim(), lambda: "x", agora=5000.0, ultimo=0.0, intervalo=1800)
    assert novo == 5000.0        # avança mesmo com falha


def test_resumo_que_levanta_nao_propaga_nem_envia(monkeypatch):
    _token_vivo(monkeypatch)
    v = _Voz()

    def _boom():
        raise RuntimeError("contagem falhou")

    novo = batimento.talvez_bater(v, _boom, agora=5000.0, ultimo=0.0, intervalo=1800)
    assert v.enviados == []
    assert novo == 5000.0


# --- contrato de envio ----------------------------------------------------

def test_prefere_metodo_publico_bater_se_existir(monkeypatch):
    _token_vivo(monkeypatch)

    class _VozNova:
        def __init__(self):
            self.batidas = []

        def bater(self, texto):
            self.batidas.append(texto)

        def _enviar(self, texto):
            raise AssertionError("deveria ter usado bater()")

    v = _VozNova()
    batimento.talvez_bater(v, lambda: "ok", agora=5000.0, ultimo=0.0, intervalo=1800)
    assert v.batidas == ["viva: ok"]
