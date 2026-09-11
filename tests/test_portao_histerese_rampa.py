"""PORTÃO DE CARGA — HISTERESE, ESCALONAMENTO e EPISÓDIO PERSISTIDO (achados da revisão r6).

(1) SEM HISTERESE NEM ESCALONAMENTO: a carga média de 1 min e o kern.memorystatus_level
    demoram a refletir motores recém-lançados. Com a carga caindo LOGO abaixo do limite,
    todas as contas livres disparavam no MESMO ciclo (o próprio teste antigo afirmava 4
    disparos na volta) — 5 a 7 Chromes numa máquina ainda carregada, o padrão do incidente
    de 10/09. Agora: em sobrecarga, só libera abaixo de um limite MENOR (histerese); na
    saída, no máximo N disparos novos por ciclo (rampa) até o represamento ser servido.
    "Adiar não é falha" continua valendo para a rampa.
(4) EPISÓDIO SÓ EM MEMÓRIA: um reinício do daemon (vigia externo/launchd) com intervalo
    menor que ATHENA_CARGA_ALERTA_S zerava o relógio — o aviso ESSENCIAL nunca saía numa
    sobrecarga longa ("0 aulas" sem alerta). Agora o início do episódio vai a um arquivo
    pequeno e o boot o retoma (e o portão nasce em sobrecarga: histerese atravessa o boot).

Sensor e relógios injetados: nenhum teste depende da carga REAL desta máquina.
"""
import asyncio
import json
import logging
import os
from types import SimpleNamespace

import pytest

from maestro import athena_local, carga, causa, disjuntor
from maestro.adaptadores import captura
from tests.test_evidencia_reap import _Alertas, _SpawnTee, _VigiaNoMundo, _Voz, _executor

HOT = "https://hotmart.com/pt-br/club/x/products/111"
MK = "https://comunidade-triade.memberkit.com.br/"
KIW = "https://dashboard.kiwify.com.br/courses"
HUB = "https://app.hub.la/user_groups/k3MArNt8bNEBfAtECsUW"

_INCIDENTE = carga.LeituraCarga(carga_1min=24.0, nucleos=8, mem_livre_pct=4.0)
_NORMAL = carga.LeituraCarga(carga_1min=3.2, nucleos=8, mem_livre_pct=45.0)


def _l(c, m=45.0):
    return carga.LeituraCarga(carga_1min=c, nucleos=8, mem_livre_pct=m)


class _Ref:
    """Leitura mutável + relógio monotônico falso, compartilhados com o portão."""
    def __init__(self, leitura, t=0.0):
        self.leitura = leitura
        self.t = t

    def sensor(self):
        return self.leitura

    def relogio(self):
        return self.t


def _portao(ref, **kw):
    return carga.PortaoCarga(sensor=ref.sensor, relogio=ref.relogio, **kw)


# ==========================================================================
# 1) HISTERESE (unidade)
# ==========================================================================
def test_histerese_da_carga_so_libera_abaixo_do_limite_de_saida():
    # 8 núcleos: entra com carga > 12,0 (1,5×8); sai só com carga <= 9,6 (0,8×12).
    ref = _Ref(_l(13.0))
    p = _portao(ref)
    assert p.avaliar(0)                                    # 13 > 12: entra
    ref.leitura = _l(11.9)
    motivo = p.avaliar(0)
    # DENTES: sem histerese, 11,9 (logo abaixo do limite) liberava todas as contas.
    assert motivo, "saiu da sobrecarga logo abaixo do limite de ENTRADA"
    assert "só libero com carga <= 9.6" in motivo
    ref.leitura = _l(9.7)
    assert p.avaliar(0)                                    # ainda acima do de saída
    ref.leitura = _l(9.6)
    assert p.avaliar(0) is None                            # <= 9,6: sai
    ref.leitura = _l(11.9)
    assert p.avaliar(0) is None                            # livre: só entra acima de 12
    ref.leitura = _l(12.1)
    assert p.avaliar(0)


def test_histerese_da_memoria_so_libera_acima_do_limite_de_saida():
    # entra com memória livre < 10%; sai só com >= 15% (mín + 5 p.p.).
    ref = _Ref(_l(1.0, 9.0))
    p = _portao(ref)
    assert p.avaliar(0)
    ref.leitura = _l(1.0, 12.0)
    assert p.avaliar(0), "memória saiu da sobrecarga logo acima do mínimo de ENTRADA"
    ref.leitura = _l(1.0, 15.0)
    assert p.avaliar(0) is None
    ref.leitura = _l(1.0, 12.0)
    assert p.avaliar(0) is None                            # livre: só entra abaixo de 10
    ref.leitura = _l(1.0, 9.9)
    assert p.avaliar(0)


def test_histerese_e_por_criterio():
    # preso SÓ pela memória: uma carga de 11 (nunca passou de 12) não segura a saída.
    ref = _Ref(_l(11.0, 8.0))
    p = _portao(ref)
    assert p.avaliar(0)
    ref.leitura = _l(11.0, 16.0)
    assert p.avaliar(0) is None


def test_sensor_ilegivel_em_sobrecarga_solta_a_trava_fail_open():
    # um sensor que quebra DURANTE a sobrecarga não pode segurar a captura para sempre.
    ref = _Ref(_INCIDENTE)
    p = _portao(ref)
    assert p.avaliar(0)
    ref.leitura = carga.LeituraCarga(None, None, None)
    assert p.avaliar(0) is None
    assert not p.em_sobrecarga()


def test_limites_de_saida_e_escalonamento_vem_do_ambiente_e_nunca_afrouxam(monkeypatch, caplog):
    monkeypatch.setenv("ATHENA_CARGA_MAX", "12")
    monkeypatch.setenv("ATHENA_CARGA_LIBERA", "10")
    monkeypatch.setenv("ATHENA_MEM_LIVRE_LIBERA_PCT", "20")
    monkeypatch.setenv("ATHENA_CARGA_DISPAROS_POR_CICLO", "3")
    p = carga.PortaoCarga.do_ambiente(sensor=lambda: _NORMAL)
    assert (p.carga_libera, p.mem_livre_libera_pct, p.disparos_por_ciclo) == (10.0, 20.0, 3)
    assert p._carga_libera_efetiva(8) == 10.0 and p._mem_libera_efetiva() == 20.0
    # limite de SAÍDA mais frouxo que o de ENTRADA é grampeado (histerese nunca negativa)
    monkeypatch.setenv("ATHENA_CARGA_LIBERA", "50")
    monkeypatch.setenv("ATHENA_MEM_LIVRE_LIBERA_PCT", "5")
    p = carga.PortaoCarga.do_ambiente(sensor=lambda: _NORMAL)
    assert p._carga_libera_efetiva(8) == 12.0 and p._mem_libera_efetiva() == 10.0
    # lixo => defaults + WARNING
    monkeypatch.setenv("ATHENA_CARGA_LIBERA", "abc")
    monkeypatch.setenv("ATHENA_CARGA_DISPAROS_POR_CICLO", "x")
    with caplog.at_level(logging.WARNING, logger="athena.carga"):
        p = carga.PortaoCarga.do_ambiente(sensor=lambda: _NORMAL)
    assert p.carga_libera is None and p.disparos_por_ciclo == carga.DISPAROS_POR_CICLO_PADRAO
    assert any("ATHENA_CARGA_LIBERA" in r.getMessage() for r in caplog.records)
    assert "histerese" in p.descrever() and "por ciclo" in p.descrever()


# ==========================================================================
# 2) ESCALONAMENTO (unidade)
# ==========================================================================
def _sair_da_sobrecarga(ref, p):
    ref.leitura = _INCIDENTE
    assert p.decidir(0) is not None
    ref.leitura = _NORMAL


def test_rampa_no_maximo_n_disparos_novos_por_ciclo_na_saida():
    ref = _Ref(_NORMAL)
    p = _portao(ref, disparos_por_ciclo=2)
    _sair_da_sobrecarga(ref, p)
    assert p.decidir(0) is None
    p.registrar_disparo()
    assert p.decidir(1) is None
    p.registrar_disparo()
    veto = p.decidir(2)
    # DENTES: sem escalonamento, o 3º (e o 4º, 5º...) subiam no MESMO ciclo.
    assert veto is not None and veto.escalonamento is True
    assert "máx 2 por ciclo" in veto.motivo
    p.novo_ciclo()                                         # ciclo seguinte: +2
    assert p.decidir(2) is None
    p.registrar_disparo()
    assert p.decidir(3) is None
    p.registrar_disparo()
    assert p.decidir(4).escalonamento
    p.novo_ciclo()
    assert p.decidir(4) is None                            # represamento servido:
    p.registrar_disparo()                                  # uma janela SEM adiamento...
    p.novo_ciclo()                                         # ... encerra a rampa
    assert not p.em_rampa()
    for n in range(6):                                     # volta ao normal: sem teto
        assert p.decidir(5 + n) is None
        p.registrar_disparo()


def test_maquina_sa_nunca_e_escalonada():
    # fora da SAÍDA da sobrecarga nada muda: 10 disparos no mesmo ciclo passam.
    ref = _Ref(_NORMAL)
    p = _portao(ref, disparos_por_ciclo=1)
    for n in range(10):
        assert p.decidir(n) is None
        p.registrar_disparo()


def test_disparos_por_ciclo_zero_desliga_o_escalonamento():
    ref = _Ref(_NORMAL)
    p = _portao(ref, disparos_por_ciclo=0)
    _sair_da_sobrecarga(ref, p)
    for n in range(6):
        assert p.decidir(n) is None
        p.registrar_disparo()


def test_sobrecarga_de_volta_na_rampa_tem_precedencia():
    ref = _Ref(_NORMAL)
    p = _portao(ref, disparos_por_ciclo=1)
    _sair_da_sobrecarga(ref, p)
    p.decidir(0)
    p.registrar_disparo()
    ref.leitura = _INCIDENTE
    veto = p.decidir(1)
    assert veto is not None and veto.escalonamento is False
    assert veto.motivo.startswith("máquina sobrecarregada")


def test_janela_da_rampa_vira_sozinha_sem_novo_ciclo():
    # um chamador que nunca sinaliza o ciclo não pode transformar a rampa em trava eterna.
    ref = _Ref(_NORMAL, t=100.0)
    p = _portao(ref, disparos_por_ciclo=1)
    _sair_da_sobrecarga(ref, p)
    p.decidir(0)
    p.registrar_disparo()
    assert p.decidir(1).escalonamento
    ref.t += carga.JANELA_CICLO_MAX_S
    assert p.decidir(1) is None                            # a janela virou: +1


def test_retomar_sobrecarga_nasce_preso_e_solta_so_abaixo_da_saida():
    ref = _Ref(_l(11.0))                                   # entre 9,6 e 12
    p = _portao(ref)
    p.retomar_sobrecarga()
    assert p.decidir(0) is not None                        # histerese atravessa o boot
    ref.leitura = _l(9.0)
    assert p.decidir(0) is None and p.em_rampa()


# ==========================================================================
# 3) NO EXECUTOR: a rampa levanta DisparoEscalonado, antes de lock/spawn
# ==========================================================================
def test_executor_rampa_nao_spawna_e_e_adiamento(tmp_path):
    ref = _Ref(_NORMAL)
    cursos = [captura.CursoLocal(KIW, "k", "kiwify"), captura.CursoLocal(HUB, "h", "hubla")]
    sp = _SpawnTee(["", ""])
    ex = _executor(tmp_path, cursos, sp)
    ex._portao_carga = _portao(ref, disparos_por_ciclo=1)
    _sair_da_sobrecarga(ref, ex._portao_carga)
    assert ex.disparar(KIW).startswith("local_iniciada")
    with pytest.raises(captura.DisparoEscalonado) as e:
        ex.disparar(HUB)
    assert isinstance(e.value, captura.MaquinaSobrecarregada)
    assert isinstance(e.value, captura.ContaOcupada)
    assert e.value.escalonamento is True and len(sp.calls) == 1
    assert len(os.listdir(tmp_path / "locks")) == 1        # só o lock do KIW
    ex.novo_ciclo()
    assert ex.disparar(HUB).startswith("local_iniciada")


# ==========================================================================
# 4) NO LOOP: carga oscilando perto do limite (o cenário da revisão)
# ==========================================================================
class _Espinha:
    def __init__(self):
        self.regs = []

    def registrar_decisao(self, o_que, por_que, **kw):
        self.regs.append((o_que, por_que, kw))


class _AlertasCarga(_Alertas):
    def __init__(self):
        super().__init__()
        self.carga = []

    def maquina_sobrecarregada(self, motivo, **kw):
        self.carga.append((motivo, kw))


def _cursos():
    return [captura.CursoLocal(HOT, "hotmart-principal", "hotmart", total_esperado=18),
            captura.CursoLocal(MK, "memberkit-triade", "memberkit", total_esperado=18),
            captura.CursoLocal(KIW, "kiwify-principal", "kiwify", total_esperado=18),
            captura.CursoLocal(HUB, "hubla-principal", "hubla", total_esperado=18)]


def _loop(tmp_path, ref, **kw_portao):
    cursos = _cursos()
    sp = _SpawnTee([""] * 10)
    ex = _executor(tmp_path, cursos, sp)
    ex._portao_carga = _portao(ref, **kw_portao)
    alertas, voz, esp, estado, voo, estado_carga = (_AlertasCarga(), _Voz(), _Espinha(),
                                                    {}, {}, {})
    kw = dict(disjuntor=disjuntor, vigia=_VigiaNoMundo(sp.vivo), causa=causa,
              alertas=alertas, lock_dir=str(tmp_path / "locks"),
              autopsia_dir=str(tmp_path / "aut"), boot_ts=0.0, espinha=esp,
              estado_carga=estado_carga)

    def ciclo(agora):
        athena_local.ciclo_local(cursos, ex, lambda c: (0, 18), voz, voo, estado,
                                 agora=agora, **kw)

    return cursos, sp, alertas, voz, esp, estado, estado_carga, ciclo


def test_carga_oscilando_logo_abaixo_do_limite_nao_solta_todas_as_contas(tmp_path):
    ref = _Ref(_l(13.0))
    cursos, sp, alertas, voz, esp, estado, estado_carga, ciclo = _loop(tmp_path, ref)
    ciclo(1000.0)
    assert sp.calls == []
    ref.leitura = _l(11.8)                                 # logo abaixo de 12
    ciclo(1120.0)
    # DENTES: sem histerese => os 4 Chromes subiam aqui, numa máquina com carga 11,8.
    assert sp.calls == []
    ref.leitura = _l(12.5)
    ciclo(1240.0)
    ref.leitura = _l(9.0)                                  # aliviou de verdade
    ciclo(1360.0)
    # DENTES: sem escalonamento => 4 aqui.
    assert len(sp.calls) == 2
    ciclo(1480.0)
    assert len(sp.calls) == 4
    for c in cursos:                                       # nada disso é falha
        st = estado[c.url]
        assert st.get("disj_falhas", 0) == 0 and st["tentativas"] == 1
    assert alertas.mortes == [] and voz.escaladas == []
    # a rampa aparece SÓ como aviso agregado não-essencial (nunca por curso)
    rampa = [(m, kw) for m, kw in alertas.carga if "escalonado" in m]
    assert rampa and all(kw.get("essencial") is False for _m, kw in rampa)
    assert "desde" not in estado_carga                     # e não é episódio de sobrecarga


def test_rampa_nao_conta_como_sobrecarga_no_episodio(tmp_path, monkeypatch):
    # 2 ciclos de sobrecarga e depois 5 de rampa (1 por ciclo): o aviso de PERSISTÊNCIA
    # (limiar curto aqui) nunca pode ser disparado pela rampa — a máquina já aliviou.
    monkeypatch.setattr(athena_local, "_CARGA_ALERTA_S", 300.0)
    ref = _Ref(_INCIDENTE)
    cursos, sp, alertas, voz, esp, estado, estado_carga, ciclo = _loop(
        tmp_path, ref, disparos_por_ciclo=1)
    ciclo(1000.0)
    ciclo(1120.0)
    ref.leitura = _NORMAL
    t = 1240.0
    for _ in range(5):
        ciclo(t)
        t += 120.0
    assert len(sp.calls) == 4
    assert [kw for _m, kw in alertas.carga if kw.get("essencial")] == []
    assert [r for r in esp.regs if r[2].get("tipo") == "escalada"] == []


# ==========================================================================
# 5) EPISÓDIO PERSISTIDO (item 4): o aviso ESSENCIAL atravessa reinícios
# ==========================================================================
class _Relogio:
    def __init__(self, t0):
        self.t = float(t0)

    def time(self):
        return self.t

    def avancar(self, s):
        self.t += s


def _encarnacao(tmp_path, ref, rel, *, ciclos, path, alertas, espinha):
    """Um processo do daemon: executor/portão/estado NOVOS (memória zerada), mesmo disco."""
    cursos = _cursos()
    sp = _SpawnTee([""] * 10)
    ex = _executor(tmp_path, cursos, sp)
    ex._portao_carga = carga.PortaoCarga(sensor=ref.sensor)

    async def _dormir(s):
        rel.avancar(s)

    asyncio.run(athena_local.rodar(
        cursos, ex, lambda c: (0, 18), _Voz(), sleep=_dormir, intervalo_s=120.0,
        max_iters=ciclos, disjuntor=disjuntor, vigia=_VigiaNoMundo(sp.vivo), causa=causa,
        alertas=alertas, espinha=espinha, lock_dir=str(tmp_path / "locks"),
        autopsia_dir=str(tmp_path / "aut"), boot_ts=0.0, carga_episodio_path=path))
    return sp


def test_episodio_sobrevive_ao_reinicio_e_o_aviso_essencial_sai(tmp_path, monkeypatch):
    monkeypatch.setattr(athena_local, "_CARGA_ALERTA_S", 3600.0)
    rel = _Relogio(1_788_000_000.0)
    monkeypatch.setattr(athena_local, "time", SimpleNamespace(time=rel.time))
    path = str(tmp_path / "estado" / "carga_episodio.json")
    ref = _Ref(_INCIDENTE)
    # encarnação 1: 40 min de sobrecarga e o daemon é morto (vigia externo)
    a1, e1 = _AlertasCarga(), _Espinha()
    sp1 = _encarnacao(tmp_path, ref, rel, ciclos=20, path=path, alertas=a1, espinha=e1)
    assert sp1.calls == []
    assert [kw for _m, kw in a1.carga if kw.get("essencial")] == []     # < 1 h: só-log
    salvo = json.load(open(path))
    assert salvo["desde"] == 1_788_000_000.0 and salvo["escalado"] is False
    rel.avancar(45.0)                                      # ThrottleInterval do launchd
    # encarnação 2: mais 30 min de sobrecarga — o episódio tem 70 min
    a2, e2 = _AlertasCarga(), _Espinha()
    sp2 = _encarnacao(tmp_path, ref, rel, ciclos=15, path=path, alertas=a2, espinha=e2)
    assert sp2.calls == []
    essenciais = [m for m, kw in a2.carga if kw.get("essencial")]
    # DENTES: com o episódio só em memória, a 2ª encarnação recomeçava do zero (30 min <
    # 1 h) e o aviso essencial NUNCA saía.
    assert essenciais, "o reinício zerou o relógio da sobrecarga"
    assert "adiando há 6" in essenciais[0]                 # 60+ min desde a encarnação 1
    esc = [r for r in e2.regs if r[2].get("tipo") == "escalada"]
    assert len(esc) == 1
    assert json.load(open(path))["escalado"] is True
    # a sobrecarga passa: o episódio fecha e o arquivo some
    ref.leitura = _NORMAL
    a3, e3 = _AlertasCarga(), _Espinha()
    _encarnacao(tmp_path, ref, rel, ciclos=3, path=path, alertas=a3, espinha=e3)
    assert not os.path.exists(path)


def test_escalada_da_espinha_nao_repete_depois_do_reinicio(tmp_path, monkeypatch):
    monkeypatch.setattr(athena_local, "_CARGA_ALERTA_S", 600.0)
    rel = _Relogio(1_788_000_000.0)
    monkeypatch.setattr(athena_local, "time", SimpleNamespace(time=rel.time))
    path = str(tmp_path / "carga_episodio.json")
    ref = _Ref(_INCIDENTE)
    e1 = _Espinha()
    _encarnacao(tmp_path, ref, rel, ciclos=8, path=path, alertas=_AlertasCarga(), espinha=e1)
    assert len([r for r in e1.regs if r[2].get("tipo") == "escalada"]) == 1
    e2 = _Espinha()
    a2 = _AlertasCarga()
    _encarnacao(tmp_path, ref, rel, ciclos=3, path=path, alertas=a2, espinha=e2)
    assert [r for r in e2.regs if r[2].get("tipo") == "escalada"] == []  # 1 por episódio
    assert [kw for _m, kw in a2.carga if kw.get("essencial")]           # o aviso segue


def test_reinicio_no_meio_da_sobrecarga_mantem_a_histerese(tmp_path, monkeypatch):
    rel = _Relogio(1_788_000_000.0)
    monkeypatch.setattr(athena_local, "time", SimpleNamespace(time=rel.time))
    path = str(tmp_path / "carga_episodio.json")
    ref = _Ref(_l(13.0))
    _encarnacao(tmp_path, ref, rel, ciclos=2, path=path, alertas=_AlertasCarga(),
                espinha=_Espinha())
    ref.leitura = _l(11.0)                                 # entre os dois limites
    sp = _encarnacao(tmp_path, ref, rel, ciclos=1, path=path, alertas=_AlertasCarga(),
                     espinha=_Espinha())
    # DENTES: sem retomar o portão preso, o boot lia 11 < 12 e soltava as 4 contas.
    assert sp.calls == []
    ref.leitura = _l(9.0)
    sp = _encarnacao(tmp_path, ref, rel, ciclos=1, path=path, alertas=_AlertasCarga(),
                     espinha=_Espinha())
    assert len(sp.calls) == 2                              # saiu: rampa (2 por ciclo)


def test_episodio_velho_ou_ilegivel_e_descartado(tmp_path):
    path = tmp_path / "carga_episodio.json"
    agora = 1_788_000_000.0
    # a última observação ficou a 2 h: o loop ficou fora do ar — não é "o mesmo episódio"
    path.write_text(json.dumps({"desde": agora - 5 * 3600, "ultimo": agora - 2 * 3600,
                                "escalado": True}))
    assert athena_local._carregar_episodio_carga(str(path), agora=agora) == {}
    assert not path.exists()
    path.write_text("{lixo")
    assert athena_local._carregar_episodio_carga(str(path), agora=agora) == {}
    assert not path.exists()
    path.write_text(json.dumps({"desde": agora + 7200, "ultimo": agora + 7200}))
    assert athena_local._carregar_episodio_carga(str(path), agora=agora) == {}  # futuro
    assert athena_local._carregar_episodio_carga(str(tmp_path / "nao-existe"),
                                                 agora=agora) == {}
    assert athena_local._carregar_episodio_carga(None, agora=agora) == {}
    path.write_text(json.dumps({"desde": agora - 3000, "ultimo": agora - 100,
                                "escalado": False}))
    assert athena_local._carregar_episodio_carga(str(path), agora=agora) == {
        "desde": agora - 3000, "ultimo": agora - 100, "escalado": False}


def test_disco_quebrado_nao_derruba_o_ciclo(tmp_path, caplog):
    # o diretório de estado ilegível/inescrevível: o ciclo segue e o log avisa.
    bloqueio = tmp_path / "arquivo"
    bloqueio.write_text("x")
    path = str(bloqueio / "sub" / "carga_episodio.json")   # pai é arquivo: makedirs falha
    ec = {}
    with caplog.at_level(logging.WARNING, logger="athena.local"):
        athena_local._avisar_adiamentos([(HOT, "máquina sobrecarregada (x)", False)],
                                        alertas=_AlertasCarga(), espinha=_Espinha(),
                                        agora=1000.0, estado_carga=ec, episodio_path=path)
    assert ec["desde"] == 1000.0
    assert any("não gravei o episódio" in r.getMessage() for r in caplog.records)
