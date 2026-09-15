"""D3r3 — os PORTADORES da prova do YouTube (revisão do motor M4 sobre o 99287e9).

ITEM 1: a revisão do M4 deu NO-GO pelo lado do motor — na prova, a Entrega Digital e a Hubla
continuam pedindo ao YouTube depois do bloqueio. Elas saem da lista de portadores até o motor
provar "prova ED/Hubla com 3 aulas bot → exatamente 1 pedido, linha `bloqueado`, exit 4 youtube".
Continuam portadores: `--youtube` do Hotmart, memberkit, greenn, nutror e alpaclass.
  - com a janela vencida, um curso ED ou Hubla dispara SEM token e não reserva a prova;
  - durante a suspensão, o passe normal delas sai sem token (o M4 barra o YouTube no motor) e a
    saída limpa sem avanço é o cooldown de sempre — nunca a espera neutra de portador.

Dublês: os do D3 (`_Mac3`: o LocalExecutor REAL com spawn de mentira e o ciclo REAL do daemon),
com cursos extras.
"""
import pytest

from maestro import athena_local
from maestro.adaptadores import captura
from tests.test_antiban_d3_prova_youtube import (  # noqa: F401
    CIRO, ENV_PROVA, H, KW, MK, OUTRO, _abrir_suspensao, _linha, _Mac3, _STATS_OK)
from tests.test_antiban_r2_suspensao_youtube import (  # noqa: F401 (fixture autouse)
    _ABORTO_YOUTUBE, _passe_youtube_ligado)
from tests.test_antiban_relancamento_exit4 import _sem_tracker_vivo  # noqa: F401
from tests.test_executor_local import DIR, PY
from tests.test_revisao_d3_reinicio_na_prova import _reiniciar

ED = "https://luanacarolina.entregadigital.app.br/"
HB = "https://app.hub.la/m/curso-hubla"
_EXTRAS = {ED: ("ed-principal", "entregadigital"), HB: ("hubla-principal", "hubla")}


class _MacCom(_Mac3):
    """O Mac do D3 com os cursos Entrega Digital e Hubla."""

    def __init__(self, tmp_path, pend):
        super().__init__(tmp_path, pend)
        for url, (conta, plat) in _EXTRAS.items():
            self.cursos.append(captura.CursoLocal(
                url=url, conta=conta, plataforma=plat, total_esperado=100,
                session_path=str(tmp_path / f"{conta}-session.json")))
        self.ex = captura.LocalExecutor(
            self.cursos, motor_python=PY, motor_dir=DIR, spawn=self.sp,
            lock_dir=str(tmp_path / "locks"), pid_vivo=self.sp.mundo.vivo,
            motor_log_dir=str(tmp_path / "logs"), pendencias_fn=self._pendencias,
            estado_cursos_path=self.path)

    def terminar(self, url, codigo, saida):
        import os
        call = self.chamadas(url)[-1]
        path = self.ex._stderr_path(self.ex.conta_de(url))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(saida)
        call["proc"].encerrar(codigo)


@pytest.mark.parametrize("url", [ED, HB], ids=["entregadigital", "hubla"])
def test_entregadigital_e_hubla_nao_levam_a_prova_com_a_janela_vencida(tmp_path, url):
    mac = _MacCom(tmp_path, {CIRO: {"youtube": 15}, url: {"base": 3}, MK: {"base": 3}})
    t = _abrir_suspensao(mac) + 3 * H + 60                 # a janela venceu
    mac.ciclo(t, [url])
    call = mac.chamadas(url)[-1]
    assert ENV_PROVA not in call["env"], f"{url} levou o token da prova"
    assert "prova_curso" not in mac.disco(), f"{url} reservou a prova: {mac.disco()}"
    mac.ciclo(t + 180, [MK])                               # a prova segue esperando um portador
    assert ENV_PROVA in mac.chamadas(MK)[-1]["env"] and mac.disco().get("prova_curso") == MK


@pytest.mark.parametrize("url", [ED, HB], ids=["entregadigital", "hubla"])
def test_entregadigital_e_hubla_na_suspensao_saem_para_o_cooldown_e_nao_esperam_a_janela(tmp_path,
                                                                                         url):
    mac = _MacCom(tmp_path, {CIRO: {"youtube": 15}, url: {"base": 3}})
    t_aut = _abrir_suspensao(mac)                          # janela de 3 h aberta
    mac.ciclo(t_aut + 120, [url])
    assert len(mac.chamadas(url)) == 1 and ENV_PROVA not in mac.chamadas(url)[-1]["env"]
    mac.terminar(url, 0, "Stats: total=3 ok=0 audio=0 falhou=3\n")     # o M4 barrou o YouTube
    mac.ciclo(t_aut + 240, [url])
    st = mac.estado[url]
    assert not st.get("espera_youtube"), f"{url} virou portador em espera pela janela: {st}"
    assert st.get("cooldown_ate") == t_aut + 240 + 6 * H, st
    mac.ciclo(t_aut + 3 * H + 60, [url])                   # vencida a janela: não sai (cooldown)...
    assert len(mac.chamadas(url)) == 1
    mac.ciclo(t_aut + 240 + 6 * H + 60, [url])             # ... e volta quando o cooldown vence
    assert len(mac.chamadas(url)) == 2 and ENV_PROVA not in mac.chamadas(url)[-1]["env"]


# ==========================================================================
# ITEM 2: o rodízio do portador
#   - contador por curso de sem_sucesso SEGUIDOS na prova; só ok e bloqueado zeram (nao_tocou não);
#   - 3 seguidos -> 24 h fora da lista de PORTADORES (os passes normais seguem);
#   - depois de qualquer resultado não-ok, a próxima prova prefere OUTRO portador, se houver;
#   - sem outro portador possível: a suspensão segue e a voz avisa uma vez por episódio;
#   - contador e exclusão persistidos (atravessam o reinício).
# ==========================================================================
_PEND_RODIZIO = {CIRO: {"youtube": 15}, OUTRO: {"youtube": 8}, KW: {"base": 3}}
_SEM_SUCESSO = "Stats: total=15 ok=0 audio=0 falhou=15\n" + _linha("sem_sucesso")
_NAO_TOCOU = "Stats: total=15 ok=0 audio=0 falhou=0\n" + _linha("nao_tocou")
_PRESA = "presa em sem_sucesso"


def _levar_prova(mac, url, desde, cursos, *, passo=1800, limite=120):
    """Ciclos a cada `passo` a partir de `desde` até `url` disparar COM o token; devolve o instante."""
    t = desde
    for _ in range(limite):
        antes = len(mac.chamadas(url))
        mac.ciclo(t, cursos)
        if any(ENV_PROVA in c["env"] for c in mac.chamadas(url)[antes:]):
            return t
        t += passo
    raise AssertionError(f"{url} não levou a prova em {limite} ciclos: {mac.disco()}")


def _prova_com(mac, url, desde, cursos, saida, codigo=0):
    """`url` leva a prova, ela sai com `saida`, e a autópsia do ciclo seguinte a lê; devolve o
    instante da autópsia."""
    t = _levar_prova(mac, url, desde, cursos)
    mac.terminar(url, codigo, saida)
    mac.ciclo(t + 180, cursos)
    return t + 180


def _nunca_leva(mac, url, desde, ate, cursos, *, passo=1800):
    """Ciclos de `desde` até `ate`: `url` nunca dispara com o token; devolve o próximo instante."""
    t = desde
    while t < ate:
        antes = len(mac.chamadas(url))
        mac.ciclo(t, cursos)
        com_token = [c["cmd"] for c in mac.chamadas(url)[antes:] if ENV_PROVA in c["env"]]
        assert com_token == [], f"{url} levou a prova às {t}: {mac.disco()}"
        t += passo
    return t


# a) três sem_sucesso seguidos no mesmo curso -> a 4ª prova vai para outro
def test_tres_sem_sucesso_seguidos_tiram_o_curso_da_prova_e_a_quarta_vai_para_outro(tmp_path):
    mac = _Mac3(tmp_path, _PEND_RODIZIO)
    t = _abrir_suspensao(mac) + 3 * H + 60
    t = _prova_com(mac, CIRO, t, [CIRO], _SEM_SUCESSO)
    t = _prova_com(mac, CIRO, t, [CIRO], _SEM_SUCESSO)
    mac = _reiniciar(mac, _PEND_RODIZIO, t + 60)           # o contador atravessa o reinício
    assert mac.estado[CIRO].get("prova_sem_sucesso_seguidas") == 2, mac.estado[CIRO]
    t = _prova_com(mac, CIRO, t + 120, [CIRO], _SEM_SUCESSO)
    st = mac.estado[CIRO]
    assert st.get("prova_sem_sucesso_seguidas") == 3 and st.get("prova_fora_ate") == t + 24 * H, st
    # sozinho e dentro das 24 h, o Ciro não leva a prova — nem depois de um ciclo recusado
    fim = _nunca_leva(mac, CIRO, t + 180, t + 20 * H, [CIRO])
    # a 4ª prova, com o Ciro PRIMEIRO na lista e fora do cooldown, vai para o outro curso
    _levar_prova(mac, OUTRO, fim, [CIRO, OUTRO])
    assert mac.disco().get("prova_curso") == OUTRO, mac.disco()


# b) nao_tocou não zera: alternar sem_sucesso e nao_tocou não devolve a prova ao mesmo curso
def test_alternar_sem_sucesso_e_nao_tocou_nao_zera_o_contador_nem_devolve_a_prova_ao_curso(
        tmp_path):
    mac = _Mac3(tmp_path, _PEND_RODIZIO)
    t = _abrir_suspensao(mac) + 3 * H + 60
    for saida, seguidas in ((_SEM_SUCESSO, 1), (_NAO_TOCOU, 1), (_SEM_SUCESSO, 2),
                            (_NAO_TOCOU, 2), (_SEM_SUCESSO, 3)):
        t = _prova_com(mac, CIRO, t, [CIRO], saida)
        assert mac.estado[CIRO].get("prova_sem_sucesso_seguidas") == seguidas, mac.estado[CIRO]
    fim = _nunca_leva(mac, CIRO, t + 180, t + 20 * H, [CIRO])
    _levar_prova(mac, OUTRO, fim, [CIRO, OUTRO])


# c) o curso fora da prova segue disparando os passes normais, sem cooldown, bench nem espera
def test_o_curso_fora_do_rodizio_segue_disparando_os_passes_normais(tmp_path):
    pend = {CIRO: {"youtube": 15, "audio": 4}, MK: {"base": 3}, OUTRO: {"youtube": 8}}
    mac = _Mac3(tmp_path, pend)
    t = _abrir_suspensao(mac) + 3 * H + 60                 # janela vencida: a prova está armada
    for url in (CIRO, MK):
        mac.estado.setdefault(url, {}).update(prova_sem_sucesso_seguidas=3,
                                              prova_fora_ate=t + 20 * H)
    mac.ex._passe_cursor["hotmart-principal"] = 4          # o anel do Ciro aponta o --youtube
    n_ciro = len(mac.chamadas(CIRO))
    mac.ciclo(t, [CIRO, MK, OUTRO])
    assert len(mac.chamadas(CIRO)) == n_ciro + 1, "o curso fora da prova parou de disparar"
    ciro, mk = mac.chamadas(CIRO)[-1], mac.chamadas(MK)[-1]
    assert "--audio" in ciro["cmd"] and ENV_PROVA not in ciro["env"], ciro["cmd"]
    assert ENV_PROVA not in mk["env"], "o Memberkit fora do rodízio levou a prova"
    assert ENV_PROVA in mac.chamadas(OUTRO)[-1]["env"] and mac.disco().get("prova_curso") == OUTRO
    for url in (CIRO, MK):
        st = mac.estado[url]
        assert not ({"cooldown_ate", "espera_youtube", "benched_exit4", "irredutivel"} & set(st)), st


# d) sem outro portador possível, o aviso sai uma vez por episódio (e não repete no reinício)
def test_sem_outro_portador_a_prova_presa_avisa_uma_vez(tmp_path):
    pend = {CIRO: {"youtube": 15}, KW: {"base": 3}}        # Kiwify não porta a prova
    mac = _Mac3(tmp_path, pend)
    t = _abrir_suspensao(mac) + 3 * H + 60
    for _ in range(3):
        t = _prova_com(mac, CIRO, t, [CIRO, KW], _SEM_SUCESSO)
    assert mac.avisos(_PRESA) == []                        # a janela reaberta ainda vale
    fim = _nunca_leva(mac, CIRO, t + 180, t + 3 * H + 2 * 3600, [CIRO, KW])
    aviso = f"prova do YouTube presa em sem_sucesso em {CIRO} — nenhum outro portador"
    assert mac.avisos(_PRESA) == [aviso], mac.voz.escaladas
    assert "prova_curso" not in mac.disco() and mac.disco().get("ate") is not None
    mac2 = _reiniciar(mac, pend, fim)
    _nunca_leva(mac2, CIRO, fim + 60, fim + 6 * H, [CIRO, KW])
    assert mac2.avisos(_PRESA) == [], "o mesmo episódio avisou de novo depois do reinício"


def test_com_outro_portador_possivel_mesmo_ocupado_a_prova_presa_nao_avisa(tmp_path):
    pend = {CIRO: {"youtube": 15}, MK: {"base": 3}}
    mac = _Mac3(tmp_path, pend)
    t = _abrir_suspensao(mac) + 3 * H + 60
    for _ in range(3):
        t = _prova_com(mac, CIRO, t, [CIRO], _SEM_SUCESSO)
    mac.ciclo(t + 180, [CIRO, MK])                         # janela aberta: o Memberkit roda sem token
    assert len(mac.chamadas(MK)) == 1 and ENV_PROVA not in mac.chamadas(MK)[-1]["env"]
    _nunca_leva(mac, CIRO, t + 360, t + 3 * H + 2 * 3600, [CIRO, MK])
    assert mac.avisos(_PRESA) == [], "avisou 'nenhum outro portador' com o Memberkit na lista"


# e) ok e bloqueado zeram o contador
@pytest.mark.parametrize("resultado", ["ok", "bloqueado"])
def test_ok_e_bloqueado_zeram_o_contador_de_sem_sucesso(tmp_path, resultado):
    mac = _Mac3(tmp_path, _PEND_RODIZIO)
    t = _abrir_suspensao(mac) + 3 * H + 60
    t = _prova_com(mac, CIRO, t, [CIRO], _SEM_SUCESSO)
    t = _prova_com(mac, CIRO, t, [CIRO], _SEM_SUCESSO)
    assert mac.estado[CIRO].get("prova_sem_sucesso_seguidas") == 2
    if resultado == "ok":
        t = _prova_com(mac, CIRO, t, [CIRO], _STATS_OK + _linha("ok"))
    else:
        t = _prova_com(mac, CIRO, t, [CIRO], _ABORTO_YOUTUBE + _linha("bloqueado"), codigo=4)
    assert "prova_sem_sucesso_seguidas" not in mac.estado[CIRO], mac.estado[CIRO]
    disco = athena_local._carregar_estado_cursos(mac.path, agora=t).get(CIRO) or {}
    assert "prova_sem_sucesso_seguidas" not in disco, disco
    if resultado == "bloqueado":                           # a suspensão seguiu, no degrau 2
        t = _prova_com(mac, CIRO, t, [CIRO], _SEM_SUCESSO)
        st = mac.estado[CIRO]
        assert st.get("prova_sem_sucesso_seguidas") == 1 and "prova_fora_ate" not in st, st


# preferir OUTRO portador depois de um não-ok — e, sem outro, não deixar a suspensão sem prova
def test_depois_de_um_resultado_nao_ok_a_proxima_prova_prefere_outro_portador(tmp_path):
    mac = _Mac3(tmp_path, _PEND_RODIZIO)
    t = _abrir_suspensao(mac) + 3 * H + 60
    t = _prova_com(mac, CIRO, t, [CIRO], "", codigo=-15)   # SIGTERM: sem linha, sem cooldown
    t_venceu = t + 3 * H + 60
    n = len(mac.chamadas(CIRO))
    mac.ciclo(t_venceu, [CIRO, OUTRO])                     # o Ciro PRIMEIRO e livre para disparar
    assert [c for c in mac.chamadas(CIRO)[n:] if ENV_PROVA in c["env"]] == []
    assert mac.disco().get("prova_curso") == OUTRO, mac.disco()


def test_sem_outro_portador_possivel_o_ultimo_nao_ok_leva_a_prova_na_hora(tmp_path):
    mac = _Mac3(tmp_path, _PEND_RODIZIO)
    t = _abrir_suspensao(mac) + 3 * H + 60
    t = _prova_com(mac, CIRO, t, [CIRO, KW], "", codigo=-15)
    n = len(mac.chamadas(CIRO))
    mac.ciclo(t + 3 * H + 60, [CIRO, KW])                  # Kiwify não porta: sem atraso nenhum
    assert [c for c in mac.chamadas(CIRO)[n:] if ENV_PROVA in c["env"]], mac.disco()
    assert mac.disco().get("prova_curso") == CIRO


def test_com_o_outro_portador_ocupado_o_ultimo_nao_ok_leva_a_prova_no_ciclo_seguinte(tmp_path):
    mac = _Mac3(tmp_path, {CIRO: {"youtube": 15}, MK: {"base": 3}})
    t = _abrir_suspensao(mac) + 3 * H + 60
    t = _prova_com(mac, CIRO, t, [CIRO], "", codigo=-15)
    mac.ciclo(t + 180, [MK])                               # janela aberta: o Memberkit roda sem token
    assert len(mac.chamadas(MK)) == 1 and ENV_PROVA not in mac.chamadas(MK)[-1]["env"]
    t_venceu = t + 3 * H + 60
    n = len(mac.chamadas(CIRO))
    mac.ciclo(t_venceu, [CIRO, MK])                        # recusado: o Memberkit ocupado não leva
    assert len(mac.chamadas(CIRO)) == n and "prova_curso" not in mac.disco(), mac.disco()
    mac.ciclo(t_venceu + 180, [CIRO, MK])                  # ... e no ciclo seguinte o Ciro leva
    assert ENV_PROVA in mac.chamadas(CIRO)[-1]["env"] and mac.disco().get("prova_curso") == CIRO
