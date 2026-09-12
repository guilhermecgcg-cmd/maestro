"""R15 — o alarme que mente e o disjuntor que evapora.

Quatro defeitos MEDIDOS no daemon vivo (12/09), cada um com teste DE DENTES (reintroduza
o bug e o teste falha):

1. FALSO ALARME DE TOKEN. Das 23 escaladas `escalar_token` dos últimos 7 dias, 23 foram
   FALSAS: todas casaram a palavra "Forbidden" NUA vinda de uma linha do yt-dlp
   (`ERROR: unable to download video data: HTTP Error 403: Forbidden`) — a CDN do vídeo
   recusando o segmento, nunca uma chave de API. Fixtures aqui são as CAUDAS REAIS
   copiadas das autópsias em disco.
2. DISJUNTOR QUE EVAPORA. A escada (600s/1h/6h/24h) e os cooldowns viviam num dict criado
   a cada boot: qualquer reinício zerava um castigo de até 24 h e o curso voltava a ser
   martelado (anti-ban).
3. ATHENA_MAX_TENTATIVAS era código morto (o `_DisjuntorTeto` nunca é alcançado; o limiar
   real era o 3 hardcoded do módulo).
4. A mensagem do circuit-breaker manda o dono olhar "o lado da Hotmart" em runs de Greenn,
   Kiwify e Alpaclass.

O teste `test_corpus_real_*` roda o classificador contra as autópsias REAIS do disco
(só leitura); ele é pulado numa máquina que não as tem.
"""
import asyncio
import glob
import json
import os
from types import SimpleNamespace

import pytest

from maestro import athena_local, causa, disjuntor, vigia
from maestro.vigia import Obito
from tests.test_athena_integracao import (FakeExecutorObitos, FakeVoz, SpyAlertas,
                                          _curso, _prog, _reais)

C1 = "https://sierramkt.greenn.club/"
AUTOPSIAS = os.path.expanduser("~/.athena-local/autopsias")


def _obito(stderr="", exit_code=1, conta="acme", curso="http://c", flaps=1):
    return Obito(conta=conta, curso=curso, exit_code=exit_code,
                 stderr_tail=stderr, flaps_na_janela=flaps, ts="")


def _classificar(stderr, exit_code=4, curso=C1, **kw):
    # tracker_dir inexistente: o enriquecimento do exit-4 não pode tocar o tracker.db VIVO.
    return causa.classificar(_obito(stderr=stderr, exit_code=exit_code, curso=curso),
                             tracker_dir="/nao/existe/tracker", **kw)


# ==========================================================================
# 1) FALSO ALARME DE TOKEN — caudas REAIS do disco
# ==========================================================================
# Copiada VERBATIM de ~/.athena-local/autopsias/20260911T094335_207952-greenn-principal.json
# (exit 4, plataforma greenn) — uma das 23 que viraram "troque o token".
_CAUDA_YTDLP_403 = (
    "WARNING: [youtube] nsig extraction failed: Some formats may be missing\n"
    "         Install a JS runtime. n-signature extraction without a JS runtime has been "
    "deprecated, and some formats may be missing. See  "
    "https://github.com/yt-dlp/yt-dlp/wiki/EJS  for details on installing one\n"
    "ERROR: unable to download video data: HTTP Error 403: Forbidden\n"
    "WARNING: [youtube] No supported JavaScript runtime could be found. Only deno is "
    "enabled by default; to use another runtime, see the --extractor-args docs\n"
    "⛔ RUN INTERROMPIDO POR EXCESSO DE FALHAS: 3 aulas processadas e NENHUMA funcionou "
    "(nem com legenda, nem sem). Algo está sistematicamente errado do lado da Hotmart — "
    "pode ser o anti-bot, a sessão, a rede, ou um detector nosso ruim. Continuar "
    "martelando é o caminho do banimento. Parando com ok=0 audio=0 falhou=3 de 3.\n"
)

# Cauda REAL da Alpaclass (11/09 11:57): o motor RENOVA o Bearer depois de um 401 e o run
# SEGUE. "401", "token" e "Bearer" na mesma linha — e nada disso é chave de API ruim.
_CAUDA_ALPACLASS_RENOVA = (
    "2026-09-11 11:57:14,341 WARNING motor.alpaclass.api: alpaclass: a renovação "
    "antecipada não trouxe Bearer novo (a SPA não chamou POST /auth/refresh) — sigo com "
    "o atual; um 401 re-valida antes de qualquer veredito\n"
    "2026-09-11 11:57:14,607 WARNING motor.alpaclass.api: alpaclass: a API learner "
    "recusou o Bearer do run em /lessons/o9Lomu/files (401/USR_04) — relendo o token do "
    "perfil/arquivo, sondando e renovando antes de decidir\n"
    "2026-09-11 11:57:21,097 WARNING motor.alpaclass.api: alpaclass: Bearer RENOVADO "
    "adotado (renovação da SPA depois do 401) — o run segue\n"
)

# Cauda REAL do Curseduca: um 500 cuja mensagem LEVANTA A HIPÓTESE de api_key ausente.
# Hipótese não é veredito — a mesma classe do log Groq 200 do incidente de 21/07.
_CAUDA_CURSEDUCA_500 = (
    "RuntimeError: API Curseduca (clas) respondeu 500 em "
    "https://clas.curseduca.pro/showcases/1 (Bearer stale/expirado? api_key ausente? "
    "— ver recon)\n"
)

# Dump de configuração que a autópsia captura na cauda (a chave aparece, ninguém falhou).
_CAUDA_DUMP_CONFIG = (
    "  - nome: curseduca\n"
    "    api_key: 27103cf0b08c7cb0b882a492d07114dc28e2e45d\n"
    "    Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VyIjp7ImlkIjo2M\n"
)


@pytest.mark.parametrize("cauda,rotulo", [
    (_CAUDA_YTDLP_403, "yt-dlp 403 da CDN (16 Greenn + 7 Alpaclass)"),
    (_CAUDA_ALPACLASS_RENOVA, "alpaclass renovando o Bearer depois de um 401"),
    (_CAUDA_CURSEDUCA_500, "curseduca: 500 com api_key como HIPÓTESE"),
    (_CAUDA_DUMP_CONFIG, "dump de config com a chave (ninguém falhou)"),
])
def test_cauda_sem_falha_de_chave_nao_manda_trocar_o_token(cauda, rotulo):
    d = _classificar(cauda, exit_code=4)
    assert d.acao != "escalar_token", (rotulo, d)
    assert d.acao == "escalar_humano", (rotulo, d)      # o veredito honesto do exit 4
    assert d.fonte == "deterministico", d


def test_dentes_a_palavra_forbidden_nua_ainda_estaria_la_e_ja_nao_decide():
    """DENTES do item 1, em dois tempos.

    (a) A fixture EXERCITA o bug: a regex LARGA (`_RE_TOKEN`, hoje só de mascaramento —
        e a que o daemon VIVO ainda roda como classificador) casa a cauda. Se alguém
        "limpar" a fixture e ela deixar de casar, este assert avisa que o teste virou
        decoração.
    (b) E mesmo assim o classificador NÃO decide token. Reintroduzir `forbidden`/
        `unauthorized` nuas em `_sinal_de_credencial` (ou voltar o call-site para
        `_RE_TOKEN.search(err)`) faz a 2ª metade falhar na hora.
    """
    assert causa._RE_TOKEN.search(_CAUDA_YTDLP_403), "a fixture não exercita mais o bug"
    assert not causa._sinal_de_credencial(_CAUDA_YTDLP_403)
    assert _classificar(_CAUDA_YTDLP_403, exit_code=4).acao == "escalar_humano"


@pytest.mark.parametrize("cauda", [
    # Formas REAIS com que os SDKs dos provedores denunciam a chave.
    "openai.AuthenticationError: Error code: 401 - {'error': {'message': 'Incorrect API "
    "key provided: sk-proj-***', 'code': 'invalid_api_key'}}",
    "groq.AuthenticationError: Error code: 401 - {'error': {'message': 'Invalid API Key', "
    "'code': 'invalid_api_key'}}",
    "anthropic.PermissionDeniedError: Error code: 403 - {'error': {'type': "
    "'permission_error', 'message': 'Your API key does not have permission'}}",
    "GROQ_API_KEY invalid",
    "Incorrect API key provided",
    "403 Forbidden: insufficient permissions",
    "ValueError: no api key provided",
    'httpx: HTTP Request: POST https://api.x.com/v1 "HTTP/1.1 401" x-api-key rejeitada',
])
def test_falha_de_chave_de_verdade_continua_escalando_token(cauda):
    # O outro lado da moeda: apertar a regra NÃO pode deixar passar o caso legítimo.
    assert causa._sinal_de_credencial(cauda), cauda
    assert _classificar(cauda, exit_code=4).acao == "escalar_token", cauda


def test_credencial_vence_o_exit_code_do_circuit_breaker():
    # A PRECEDÊNCIA (texto > exit code) continua: um exit 4 cuja cauda DECLARA a chave
    # ruim segue virando "troque o token" — é a causa acionável. O que mudou é o que
    # conta como "declara".
    cauda = _CAUDA_YTDLP_403 + "\ngroq.AuthenticationError: Error code: 401 - invalid_api_key"
    assert _classificar(cauda, exit_code=4).acao == "escalar_token"


def test_proximidade_e_por_LINHA_nao_pela_cauda_inteira():
    """DENTES da camada (B): um 403 de CDN numa linha e um "api_key" de OUTRO log 40
    linhas adiante não podem se somar. Trocar o laço por linha por um
    `_RE_TOKEN_40X.search(err) and _RE_TOKEN_CTX.search(err)` faz este teste falhar."""
    cauda = (_CAUDA_YTDLP_403 + "\n" * 40 + _CAUDA_DUMP_CONFIG)
    assert not causa._sinal_de_credencial(cauda)
    assert _classificar(cauda, exit_code=4).acao == "escalar_humano"


def test_o_rotulo_continua_mascarando_de_forma_conservadora():
    # `maestro.rotulo` importa `_RE_TOKEN` para MASCARAR texto vindo da plataforma antes
    # de imprimir. Ali largo é BOM (defesa em profundidade) — apertar a máscara junto com
    # o classificador deixaria um título "Unauthorized Access 101" viajar cru pro alerta.
    from maestro.rotulo import MASCARA, casa_ancora_de_morte, rotulo_seguro
    for hostil in ("Unauthorized Access 101", "https://app.hub.la/g/forbidden-secrets"):
        r = rotulo_seguro(hostil)
        assert MASCARA in r, hostil
        assert not casa_ancora_de_morte(r), r


@pytest.mark.skipif(not os.path.isdir(AUTOPSIAS), reason="sem autópsias reais nesta máquina")
def test_corpus_real_nenhuma_autopsia_do_disco_vira_troca_de_token():
    """PROVA COM O DISCO (só leitura): roda o classificador contra TODAS as autópsias
    reais. Nenhuma delas é falha de chave de API — e as que hoje estão gravadas como
    `escalar_token` (23, todas dos últimos 7 dias) passam a `escalar_humano`."""
    arquivos = sorted(glob.glob(os.path.join(AUTOPSIAS, "*.json")))
    if not arquivos:
        pytest.skip("diretório de autópsias vazio")
    gravadas_token, novas_token, viraram_humano = 0, 0, 0
    for caminho in arquivos:
        try:
            with open(caminho) as f:
                d = json.load(f)
        except Exception:
            continue
        acao = _classificar(d.get("stderr_tail") or "", exit_code=d.get("exit_code"),
                            curso=d.get("curso") or "http://c").acao
        if d.get("acao") == "escalar_token":
            gravadas_token += 1
            if acao == "escalar_humano":
                viraram_humano += 1
        if acao == "escalar_token":
            novas_token += 1
    assert gravadas_token > 0, "o corpus não contém mais o incidente que este fix corrige"
    assert viraram_humano == gravadas_token, (viraram_humano, gravadas_token)
    assert novas_token == 0, f"{novas_token} caudas reais ainda mandariam trocar o token"


# ==========================================================================
# 4) A MENSAGEM DO CIRCUIT-BREAKER NOMEIA A PLATAFORMA DO RUN
# ==========================================================================
def test_exit4_nomeia_a_plataforma_do_run_e_nao_a_hotmart():
    d = _classificar(_CAUDA_YTDLP_403, exit_code=4, plataforma="greenn")
    assert "plataforma greenn" in d.motivo, d.motivo
    # DENTES: sem a nomeação, a única plataforma que o dono lê é a "Hotmart" do motor.
    assert "Hotmart" in _CAUDA_YTDLP_403 and "greenn" in d.motivo


def test_exit4_sem_rotulo_do_yaml_cai_no_host_do_curso():
    d = _classificar(_CAUDA_YTDLP_403, exit_code=4, curso="https://www.kiwify.com.br/x")
    assert "plataforma kiwify.com.br" in d.motivo, d.motivo


def test_exit4_sem_plataforma_determinavel_nao_chuta_nada():
    d = _classificar(_CAUDA_YTDLP_403, exit_code=4, curso="")
    assert "plataforma" not in d.motivo, d.motivo
    assert "hotmart" not in d.motivo.lower(), d.motivo


def test_a_plataforma_do_yaml_chega_na_autopsia_gravada(tmp_path):
    """O loop passa o rótulo do YAML na classificação (não só o host): a autópsia em
    disco — o que o dono lê depois — nomeia 'greenn', não 'Hotmart'."""
    alr = SpyAlertas()
    voz = FakeVoz()
    ex = FakeExecutorObitos({C1: "a"})
    estado, voo = {}, {}
    cursos = [_curso(C1, "a", "greenn", 18)]
    common = _reais(tmp_path, alr)
    athena_local.ciclo_local(cursos, ex, _prog({C1: (0, 18)}), voz, voo, estado,
                             agora=1000.0, **common)
    ex.matar(C1, exit_code=4, stderr=_CAUDA_YTDLP_403)
    athena_local.ciclo_local(cursos, ex, _prog({C1: (0, 18)}), voz, voo, estado,
                             agora=1001.0, **common)
    escritos = sorted(glob.glob(str(tmp_path / "aut" / "*.json")))
    assert escritos, "nenhuma autópsia gravada"
    with open(escritos[-1]) as f:
        rec = json.load(f)
    assert rec["acao"] == "escalar_humano", rec            # e não o falso escalar_token
    assert "plataforma greenn" in rec["motivo"], rec["motivo"]
    # e o alerta que chega ao dono também nomeia greenn, nunca Hotmart
    assert any("greenn" in m for _, m in alr.mortes), alr.mortes


# ==========================================================================
# 2) O DISJUNTOR SOBREVIVE AO REINÍCIO
# ==========================================================================
class _Relogio:
    def __init__(self, t0):
        self.t = float(t0)

    def time(self):
        return self.t

    def avancar(self, s):
        self.t += float(s or 0)


def _encarnacao(tmp_path, rel, *, ciclos, path, alertas, monkeypatch, matar=True):
    """UM processo do daemon: executor/estado NOVOS (memória zerada), mesmo disco."""
    monkeypatch.setattr(athena_local, "time", SimpleNamespace(time=rel.time))
    ex = FakeExecutorObitos({C1: "a"})
    cursos = [_curso(C1, "a", "greenn", 18)]

    async def _dormir(s):
        rel.avancar(s)
        if matar:
            ex.matar(C1, exit_code=-9)                     # SIGKILL: transitório, conta falha

    asyncio.run(athena_local.rodar(
        cursos, ex, _prog({C1: (0, 18)}), FakeVoz(), sleep=_dormir, intervalo_s=60.0,
        max_iters=ciclos, disjuntor=disjuntor, vigia=vigia, causa=causa, alertas=alertas,
        lock_dir=str(tmp_path / "locks"), autopsia_dir=str(tmp_path / "aut"),
        estado_cursos_path=path))
    return ex


def test_o_cooldown_do_disjuntor_atravessa_o_reinicio_do_daemon(tmp_path, monkeypatch):
    """DENTES do item 2: com o estado em disco, a 2ª encarnação NÃO martela o curso que
    está de castigo; sem ele (path=None, o comportamento pré-r15), martela.

    Encarnação 1: 6 ciclos, cada um terminando numa morte => a escada arma a janela.
    Encarnação 2 (processo NOVO, +60 s): o curso ainda está dentro da janela.
    """
    path = str(tmp_path / "estado" / "estado_cursos.json")
    rel = _Relogio(1_788_000_000.0)
    _encarnacao(tmp_path, rel, ciclos=6, path=path, alertas=SpyAlertas(),
                monkeypatch=monkeypatch)

    gravado = json.load(open(path))
    st = gravado["cursos"][C1]
    assert st["disj_falhas"] >= 3, gravado                 # a escada armou
    assert st["disj_bloqueado_ate"] > rel.time(), gravado  # e a janela ainda vale

    # --- encarnação 2: COM o estado em disco -> quieto -----------------------------
    ex2 = _encarnacao(tmp_path, rel, ciclos=1, path=path, alertas=SpyAlertas(),
                      monkeypatch=monkeypatch, matar=False)
    assert ex2.disparos == [], "o reinício martelou um curso em cooldown"

    # --- DENTES: o mesmo reinício SEM persistência (pré-r15) -> martela ------------
    ex3 = _encarnacao(tmp_path, rel, ciclos=1, path=None, alertas=SpyAlertas(),
                      monkeypatch=monkeypatch, matar=False)
    assert ex3.disparos == [C1], "sem persistência o teste não exercita o bug"


def test_a_escada_continua_de_onde_parou_depois_do_reinicio(tmp_path, monkeypatch):
    # Não basta ficar quieto: o DEGRAU tem de sobreviver, senão o castigo recomeça em
    # 600s em vez dos 6h/24h que as falhas anteriores já mereceram.
    path = str(tmp_path / "estado_cursos.json")
    rel = _Relogio(1_788_000_000.0)
    _encarnacao(tmp_path, rel, ciclos=8, path=path, alertas=SpyAlertas(),
                monkeypatch=monkeypatch)
    falhas_antes = json.load(open(path))["cursos"][C1]["disj_falhas"]
    assert falhas_antes >= 4

    rel.avancar(30 * 86400)                                # o Mac ficou dias fora do ar
    ex = _encarnacao(tmp_path, rel, ciclos=1, path=path, alertas=SpyAlertas(),
                     monkeypatch=monkeypatch, matar=False)
    assert ex.disparos == [C1]                             # a janela venceu: UMA tentativa
    st = json.load(open(path))["cursos"][C1]
    assert st["disj_falhas"] >= falhas_antes               # mas o degrau NÃO voltou a zero


def test_arquivo_corrompido_nao_derruba_o_boot(tmp_path):
    path = str(tmp_path / "estado_cursos.json")
    for lixo in ("", "{", "[]", '{"cursos": 3}', "\x00\x01binário"):
        with open(path, "w") as f:
            f.write(lixo)
        assert athena_local._carregar_estado_cursos(path, agora=1000.0) == {}, lixo


def test_valor_absurdo_no_futuro_e_descartado(tmp_path):
    # Um relógio doido / arquivo adulterado não pode benchar um curso por anos.
    path = str(tmp_path / "estado_cursos.json")
    with open(path, "w") as f:
        json.dump({"cursos": {C1: {"disj_falhas": 5, "disj_bloqueado_ate": 1e18,
                                   "cooldown_ate": 1e18}}}, f)
    st = athena_local._carregar_estado_cursos(path, agora=1_788_000_000.0)[C1]
    assert "disj_bloqueado_ate" not in st and "cooldown_ate" not in st, st
    assert st["disj_falhas"] == 5                          # o resto da entrada sobrevive
    assert disjuntor.pode_tentar(st, 1_788_000_000.0) is True


def test_inteiro_gigante_no_arquivo_nao_levanta(tmp_path):
    # JSON aceita inteiro de qualquer tamanho; `float()` de um inteiro enorme levanta
    # OverflowError. Um arquivo adulterado NUNCA pode derrubar a leitura.
    path = str(tmp_path / "estado_cursos.json")
    with open(path, "w") as f:
        f.write('{"cursos": {"%s": {"disj_falhas": %s, "cooldown_ate": 1}}}'
                % (C1, "9" * 400))
    st = athena_local._carregar_estado_cursos(path, agora=1_788_000_000.0)[C1]
    assert st == {"cooldown_ate": 1.0}, st


@pytest.mark.parametrize("entrada", [
    {"cursos": {C1: {"disj_falhas": "muitas"}}},
    {"cursos": {C1: {"disj_falhas": True}}},               # bool não é contador
    {"cursos": {C1: {"disj_falhas": -3}}},
    {"cursos": {C1: {"cooldown_ate": None}}},
    {"cursos": {C1: "não é dict"}},
    {"cursos": {"": {"disj_falhas": 3}}},
])
def test_entradas_invalidas_sao_descartadas_sem_levantar(tmp_path, entrada):
    path = str(tmp_path / "estado_cursos.json")
    with open(path, "w") as f:
        json.dump(entrada, f)
    assert athena_local._carregar_estado_cursos(path, agora=1000.0) == {}


def test_latches_NAO_atravessam_o_reinicio(tmp_path):
    """Decisão consciente: `irredutivel`/`benched_exit5` ficam FORA da lista branca.

    Com o zelador DESLIGADO por padrão, o reinício é hoje o único destravamento de um
    latch de reseed — persistí-lo transformaria "travado até o próximo boot" em "travado
    para sempre, calado". Se um dia o zelador virar padrão, esta decisão se revisita."""
    path = str(tmp_path / "estado_cursos.json")
    athena_local._gravar_estado_cursos(path, {C1: {
        "disj_falhas": 4, "disj_bloqueado_ate": 2000.0, "cooldown_ate": 3000.0,
        "_pend_no_cooldown": 7, "ultimo_no_notion": 12,
        "irredutivel": True, "benched_exit5": True, "exit5_seguidas": 3,
        "fase": "capturando", "ultima_causa": "escalar_reseed",
        "esgotado_avisado": True}})
    st = athena_local._carregar_estado_cursos(path, agora=1000.0)[C1]
    assert st == {"disj_falhas": 4, "disj_bloqueado_ate": 2000.0, "cooldown_ate": 3000.0,
                  "_pend_no_cooldown": 7, "ultimo_no_notion": 12}, st


def test_gravar_em_caminho_impossivel_nao_levanta(tmp_path):
    # Best-effort: a captura nunca pode cair porque o disco recusou um JSON de 200 bytes.
    athena_local._gravar_estado_cursos(str(tmp_path / "arq" / "x" / "e.json"), {C1: {}})
    athena_local._gravar_estado_cursos("/proc/nao/da/escrever.json", {C1: {"disj_falhas": 1}})
    assert athena_local._carregar_estado_cursos(None, agora=1.0) == {}


# ==========================================================================
# 3) ATHENA_MAX_TENTATIVAS DEIXA DE SER CÓDIGO MORTO
# ==========================================================================
def test_o_limiar_configurado_chega_ao_disjuntor_real():
    """DENTES do item 3: o limiar configurado tem de chegar às DUAS funções.

    `registrar_falha`: com limiar 5, a 3ª falha NÃO arma janela nenhuma — com o módulo nu
    (limiar 3 hardcoded) ela armaria. `pode_tentar`: um estado com 4 falhas e janela
    aberta é BLOQUEIO para o limiar 3 e CRÉDITO LIVRE para o limiar 5. Se o adaptador
    deixar de repassar `limiar=` em qualquer uma das duas, um destes asserts cai.
    """
    d5 = athena_local._DisjuntorRecozido(disjuntor, 5)
    agora = 1000.0

    st = {}
    for _ in range(3):
        d5.registrar_falha(st, agora)
    assert "disj_bloqueado_ate" not in st, st              # limiar 5: ainda de graça
    nu = {}
    for _ in range(3):
        disjuntor.registrar_falha(nu, agora)               # DENTES: o módulo nu já armou
    assert nu["disj_bloqueado_ate"] == agora + disjuntor.ESCADA_S[0], nu

    aberto = {"disj_falhas": 4, "disj_bloqueado_ate": agora + 600.0}
    assert d5.pode_tentar(aberto, agora) is True           # 4 < limiar 5: crédito livre
    assert disjuntor.pode_tentar(aberto, agora) is False   # DENTES: com o 3 nu, bloqueia

    for _ in range(2):
        d5.registrar_falha(st, agora)                      # 4ª e 5ª falha
    assert d5.pode_tentar(st, agora) is False              # a 5ª armou a janela
    assert d5.pode_tentar(st, agora + disjuntor.ESCADA_S[0] + 1) is True   # e re-arma
    d5.registrar_sucesso(st)
    assert st == {} and d5.pode_tentar(st, agora) is True  # sucesso zera a escada


def test_o_limiar_configurado_muda_o_disparo_de_ponta_a_ponta(tmp_path, monkeypatch):
    # Não basta o adaptador: o gate do loop tem de obedecê-lo. Com limiar 1, UMA falha
    # já cala o curso; com o limiar padrão (3), não.
    def _rodada(limiar):
        rel = _Relogio(1_788_000_000.0)
        monkeypatch.setattr(athena_local, "time", SimpleNamespace(time=rel.time))
        ex = FakeExecutorObitos({C1: "a"})

        async def _dormir(s):
            rel.avancar(s)
            ex.matar(C1, exit_code=-9)

        asyncio.run(athena_local.rodar(
            [_curso(C1, "a", "greenn", 18)], ex, _prog({C1: (0, 18)}), FakeVoz(),
            sleep=_dormir, intervalo_s=60.0, max_iters=3,
            disjuntor=athena_local._DisjuntorRecozido(disjuntor, limiar),
            vigia=vigia, causa=causa, alertas=SpyAlertas(),
            lock_dir=str(tmp_path / f"l{limiar}"), autopsia_dir=str(tmp_path / f"a{limiar}")))
        return ex.disparos

    assert len(_rodada(1)) == 1, "limiar 1 deveria calar o curso na 1ª falha"
    assert len(_rodada(3)) == 3, "limiar 3 deveria manter as tentativas livres"


@pytest.mark.parametrize("bruto,esperado", [
    (None, 3), ("", 3), ("   ", 3), ("7", 7), (" 7 ", 7),
    ("três", 3), ("3.5", 3), ("0", 1), ("-2", 1),          # ilegível -> default; <1 -> 1
])
def test_env_do_limiar_e_tolerante(monkeypatch, bruto, esperado):
    monkeypatch.delenv("ATHENA_MAX_TENTATIVAS", raising=False)
    if bruto is not None:
        monkeypatch.setenv("ATHENA_MAX_TENTATIVAS", bruto)
    assert athena_local._env_int("ATHENA_MAX_TENTATIVAS", 3, minimo=1) == esperado


def test_o_teto_fixo_continua_sendo_o_default_de_quem_nao_injeta():
    # `_DisjuntorTeto` não some: é o NULL-OBJECT que preserva o comportamento
    # pré-integração para quem chama `ciclo_local`/`rodar` sem injetar P4.
    teto = athena_local._DisjuntorTeto(2)
    assert teto.pode_tentar({"tentativas": 1}, 0.0) is True
    assert teto.pode_tentar({"tentativas": 2}, 0.0) is False
    assert teto.pode_tentar({"irredutivel": True}, 0.0) is False
