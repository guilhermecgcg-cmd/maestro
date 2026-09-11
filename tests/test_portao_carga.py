"""PORTÃO DE CARGA DA MÁQUINA (incidente 10/09 23:33–23:55).

EVIDÊNCIA: uma transcrição pesada de OUTRO programa (100% CPU, swap 6,8/7,2 GB, carga
média de 1 min até 24 num M1 de 8 núcleos/8 GB) + 5-7 Chromes headless fizeram o
Page.goto estourar 30 s em quase todas as frentes: 5 disjuntores abertos (Kiwify, Hubla,
Memberkit, Stoa, Kajabi). Disparar mais motores numa máquina assim só fabrica mortes.

CONTRATO:
  - antes de disparar um motor, carga 1 min > ATHENA_CARGA_MAX (default 1,5×núcleos) OU
    memória livre < ATHENA_MEM_LIVRE_MIN_PCT (default 10%) => ADIA neste ciclo, com
    decisão registrada "adiei <curso>: máquina sobrecarregada (carga X, memória livre Y%)";
  - teto opcional ATHENA_MAX_MOTORES (0 = sem teto);
  - adiamento NÃO é falha: não conta disjuntor, flap nem teto de tentativas; não gera
    alerta por curso (no máximo UM aviso agregado por ciclo, com dedup);
  - sensor que falha => portão ABERTO (fail-open: a leitura quebrada não pode parar a
    captura em silêncio), com aviso visível no log.
Sensor injetável: nenhum teste depende da carga REAL desta máquina (que roda a captura),
exceto o de fumaça do sensor real, que só confere que a leitura é plausível.
"""
import logging
import os
import sys

import pytest

from maestro import athena_local, carga, causa, disjuntor
from maestro.adaptadores import captura
from tests.test_evidencia_reap import _Alertas, _SpawnTee, _VigiaNoMundo, _Voz, _executor

HOT = "https://hotmart.com/pt-br/club/x/products/111"
MK = "https://comunidade-triade.memberkit.com.br/"
KIW = "https://dashboard.kiwify.com.br/courses"
HUB = "https://app.hub.la/user_groups/k3MArNt8bNEBfAtECsUW"

# A leitura do pior momento do incidente (carga 1 min ~24 em 8 núcleos; swap quase cheio).
_INCIDENTE = carga.LeituraCarga(carga_1min=24.0, nucleos=8, mem_livre_pct=4.0)
_NORMAL = carga.LeituraCarga(carga_1min=3.2, nucleos=8, mem_livre_pct=45.0)


def _sensor(leitura):
    return lambda: leitura


# ==========================================================================
# 1) O VEREDITO (PortaoCarga.avaliar) — puro sobre a leitura injetada
# ==========================================================================
def test_default_e_1_5x_nucleos_e_10pct_de_memoria():
    p = carga.PortaoCarga(sensor=_sensor(_NORMAL))
    assert p.avaliar(0) is None
    assert p.avaliar_leitura(carga.LeituraCarga(12.0, 8, 50.0)) is None     # = 1,5×8: ok
    assert p.avaliar_leitura(carga.LeituraCarga(12.1, 8, 50.0))              # > 12: adia
    assert p.avaliar_leitura(carga.LeituraCarga(1.0, 8, 10.0)) is None      # = 10%: ok
    assert p.avaliar_leitura(carga.LeituraCarga(1.0, 8, 9.9))                # < 10%: adia


def test_motivo_traz_carga_e_memoria_livre_no_formato_da_decisao():
    motivo = carga.PortaoCarga(sensor=_sensor(_INCIDENTE)).avaliar(0)
    assert motivo.startswith("máquina sobrecarregada (carga 24.0, memória livre 4%)"), motivo
    assert "12.0" in motivo and "10%" in motivo          # e os limites, p/ quem lê o log


@pytest.mark.parametrize("leitura", [
    carga.LeituraCarga(24.0, 8, 60.0),                   # só a carga
    carga.LeituraCarga(2.0, 8, 4.0),                     # só a memória
], ids=["carga", "memoria"])
def test_qualquer_um_dos_dois_sobrecarrega(leitura):
    assert carga.PortaoCarga(sensor=_sensor(leitura)).avaliar(0)


def test_teto_de_motores_simultaneos_opcional():
    p = carga.PortaoCarga(sensor=_sensor(_NORMAL), max_motores=3)
    assert p.avaliar(2) is None
    motivo = p.avaliar(3)
    assert motivo and "teto de 3 motor" in motivo and "3 ativo" in motivo
    assert carga.PortaoCarga(sensor=_sensor(_NORMAL), max_motores=0).avaliar(50) is None


@pytest.mark.parametrize("quebra", ["levanta", "tudo_none"])
def test_sensor_quebrado_e_fail_open_e_avisa_no_log(quebra, caplog):
    if quebra == "levanta":
        def sensor():
            raise OSError("sysctl indisponível")
    else:
        sensor = _sensor(carga.LeituraCarga(None, None, None))
    p = carga.PortaoCarga(sensor=sensor)
    with caplog.at_level(logging.WARNING, logger="athena.carga"):
        assert p.avaliar(0) is None                       # NÃO adia às cegas
        assert p.avaliar(0) is None
    avisos = [r for r in caplog.records if "portão ABERTO" in r.getMessage()]
    assert len(avisos) == 1                               # visível, mas só na TRANSIÇÃO


def test_leitura_parcial_usa_o_criterio_que_leu():
    # memória ilegível: a carga ainda adia; carga ilegível: a memória ainda adia.
    assert carga.PortaoCarga(sensor=_sensor(carga.LeituraCarga(24.0, 8, None))).avaliar(0)
    assert carga.PortaoCarga(sensor=_sensor(carga.LeituraCarga(None, 8, 3.0))).avaliar(0)


def test_do_ambiente_le_as_tres_variaveis_e_tolera_lixo(monkeypatch, caplog):
    monkeypatch.setenv("ATHENA_CARGA_MAX", "20")
    monkeypatch.setenv("ATHENA_MEM_LIVRE_MIN_PCT", "5")
    monkeypatch.setenv("ATHENA_MAX_MOTORES", "4")
    p = carga.PortaoCarga.do_ambiente(sensor=_sensor(_NORMAL))
    assert (p.carga_max, p.mem_livre_min_pct, p.max_motores) == (20.0, 5.0, 4)
    assert p.avaliar_leitura(carga.LeituraCarga(19.0, 8, 6.0)) is None
    monkeypatch.setenv("ATHENA_CARGA_MAX", "abc")
    monkeypatch.setenv("ATHENA_MEM_LIVRE_MIN_PCT", "")
    monkeypatch.setenv("ATHENA_MAX_MOTORES", "-2")
    with caplog.at_level(logging.WARNING, logger="athena.carga"):
        p = carga.PortaoCarga.do_ambiente(sensor=_sensor(_NORMAL))
    assert (p.carga_max, p.mem_livre_min_pct, p.max_motores) == (None, 10.0, 0)
    assert any("ATHENA_CARGA_MAX" in r.getMessage() for r in caplog.records)


def test_zero_desliga_cada_criterio(monkeypatch):
    monkeypatch.setenv("ATHENA_CARGA_MAX", "0")
    monkeypatch.setenv("ATHENA_MEM_LIVRE_MIN_PCT", "0")
    p = carga.PortaoCarga.do_ambiente(sensor=_sensor(_INCIDENTE))
    assert p.avaliar(0) is None


@pytest.mark.skipif(sys.platform != "darwin", reason="sensor real é o do macOS")
def test_sensor_real_do_mac_le_carga_e_memoria_plausiveis():
    # PROVA REAL (não dublê): lê o kern.memorystatus_level e o getloadavg DESTA máquina.
    lei = carga.ler_carga()
    assert lei.nucleos and lei.nucleos >= 1
    assert lei.carga_1min is not None and lei.carga_1min >= 0.0
    assert lei.mem_livre_pct is not None and 0.0 <= lei.mem_livre_pct <= 100.0


# ==========================================================================
# 2) NO EXECUTOR: o veto vem DEPOIS do guard anti-ban e ANTES de qualquer spawn
# ==========================================================================
def _ex(tmp_path, cursos, sp, leitura, **kw):
    ex = _executor(tmp_path, cursos, sp)
    ex._portao_carga = carga.PortaoCarga(sensor=_sensor(leitura), **kw)
    return ex


def test_executor_sobrecarregado_nao_spawna_nem_grava_lock(tmp_path):
    sp = _SpawnTee([])
    ex = _ex(tmp_path, [captura.CursoLocal(KIW, "k", "kiwify")], sp, _INCIDENTE)
    with pytest.raises(captura.MaquinaSobrecarregada) as e:
        ex.disparar(KIW)
    assert "máquina sobrecarregada" in e.value.motivo
    assert sp.calls == []                                  # nenhum motor subiu
    assert os.listdir(tmp_path / "locks") == []            # nem lock de intenção
    assert isinstance(e.value, captura.ContaOcupada)       # fail-safe p/ quem só conhece
                                                           # "aguarda a vez"


def test_executor_idempotente_e_anti_ban_vem_antes_do_portao(tmp_path):
    sp = _SpawnTee([""])
    ex = _ex(tmp_path, [captura.CursoLocal(KIW, "k", "kiwify"),
                        captura.CursoLocal(HUB, "k", "hubla")], sp, _NORMAL)
    ex.disparar(KIW)
    ex._portao_carga = carga.PortaoCarga(sensor=_sensor(_INCIDENTE))
    assert ex.disparar(KIW).startswith("ja_capturando")    # sem spawn: nem consulta o portão
    with pytest.raises(captura.ContaOcupada) as e:
        ex.disparar(HUB)
    assert not isinstance(e.value, captura.MaquinaSobrecarregada)   # a causa é a conta


def test_teto_de_motores_conta_os_locks_vivos(tmp_path):
    sp = _SpawnTee(["", ""])
    cursos = [captura.CursoLocal(KIW, "k", "kiwify"), captura.CursoLocal(HUB, "h", "hubla"),
              captura.CursoLocal(MK, "m", "memberkit")]
    ex = _ex(tmp_path, cursos, sp, _NORMAL, max_motores=2)
    ex.disparar(KIW)
    ex.disparar(HUB)
    assert ex.motores_ativos() == 2
    with pytest.raises(captura.MaquinaSobrecarregada) as e:
        ex.disparar(MK)
    assert "teto de 2" in e.value.motivo and len(sp.calls) == 2
    sp.calls[0]["proc"].encerrar(0)                        # um motor terminou
    assert ex.disparar(MK).startswith("local_iniciada")    # abriu vaga


def test_portao_que_levanta_nao_para_a_captura(tmp_path):
    class _Quebrado:
        def avaliar(self, n):
            raise RuntimeError("bug no portão")
    sp = _SpawnTee([""])
    ex = _executor(tmp_path, [captura.CursoLocal(KIW, "k", "kiwify")], sp)
    ex._portao_carga = _Quebrado()
    assert ex.disparar(KIW).startswith("local_iniciada")   # fail-open


# ==========================================================================
# 3) NO LOOP — DENTES DO CONTRATO: com o sensor "sobrecarregado" NENHUM disparo
#    acontece e NENHUM contador de falha sobe; volta ao normal => dispara.
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


def _loop(tmp_path, leitura_ref):
    cursos = [captura.CursoLocal(HOT, "hotmart-principal", "hotmart", total_esperado=18),
              captura.CursoLocal(MK, "memberkit-triade", "memberkit", total_esperado=18),
              captura.CursoLocal(KIW, "kiwify-principal", "kiwify", total_esperado=18),
              captura.CursoLocal(HUB, "hubla-principal", "hubla", total_esperado=18)]
    sp = _SpawnTee([""] * 10)
    ex = _executor(tmp_path, cursos, sp)
    ex._portao_carga = carga.PortaoCarga(sensor=lambda: leitura_ref[0])
    alertas, voz, esp, estado, voo, estado_carga = (_AlertasCarga(), _Voz(), _Espinha(),
                                                    {}, {}, {})
    kw = dict(disjuntor=disjuntor, vigia=_VigiaNoMundo(sp.vivo), causa=causa,
              alertas=alertas, lock_dir=str(tmp_path / "locks"),
              autopsia_dir=str(tmp_path / "aut"), boot_ts=0.0, espinha=esp,
              estado_carga=estado_carga)

    def ciclo(agora):
        athena_local.ciclo_local(cursos, ex, lambda c: (0, 18), voz, voo, estado,
                                 agora=agora, **kw)

    return cursos, sp, alertas, voz, esp, estado, voo, estado_carga, ciclo


def test_sobrecarregado_nenhum_disparo_nenhum_contador_de_falha(tmp_path):
    ref = [_INCIDENTE]
    cursos, sp, alertas, voz, esp, estado, voo, _ec, ciclo = _loop(tmp_path, ref)
    for i in range(5):
        ciclo(1000.0 + 120 * i)
    assert sp.calls == []                                  # DENTES: nenhum motor subiu
    for c in cursos:
        st = estado[c.url]
        assert st.get("disj_falhas", 0) == 0               # disjuntor intocado
        assert st.get("tentativas", 0) == 0                # teto de tentativas intocado
        assert not st.get("irredutivel")
        assert st.get("fase", captura.FASE_NOVO) == captura.FASE_NOVO
    assert not (tmp_path / "aut").exists() or os.listdir(tmp_path / "aut") == []
    assert alertas.mortes == []                            # nada de "MORREU"/flap
    assert voz.escaladas == []                             # nenhuma escalada POR CURSO
    # a decisão registrada, por curso, no formato pedido — UMA vez por episódio
    adiei = [r for r in esp.regs if r[0].startswith("adiei ")]
    assert sorted(r[2]["curso"] for r in adiei) == sorted(c.url for c in cursos)
    assert all("máquina sobrecarregada (carga 24.0, memória livre 4%)" in r[0] for r in adiei)
    assert all(r[2].get("escalada") is not True for r in adiei)
    # no máximo UM aviso agregado por ciclo (5 ciclos -> 5), com chave de dedup
    assert len(alertas.carga) == 5
    assert all(kw.get("chave") is not None for _m, kw in alertas.carga)
    assert "4 disparo(s)" in alertas.carga[0][0]
    # volta ao normal => dispara (never-stop), 1 por conta — ESCALONADO (r6): no máx 2
    # disparos novos por ciclo na saída da sobrecarga (o sensor demora a refletir os
    # recém-lançados; soltar os 4 de uma vez era o padrão do incidente).
    ref[0] = _NORMAL
    ciclo(1000.0 + 120 * 5)
    assert len(sp.calls) == 2                              # DENTES r6: antes eram 4
    ciclo(1000.0 + 120 * 6)
    assert len(sp.calls) == 4                              # o represamento foi servido
    assert all(estado[c.url]["tentativas"] == 1 for c in cursos)
    for c in cursos:                                       # a rampa também NÃO é falha
        assert estado[c.url].get("disj_falhas", 0) == 0
    assert alertas.mortes == [] and voz.escaladas == []


def test_adiamento_longo_nao_vira_estagnacao_por_curso_e_so_escala_agregado(tmp_path,
                                                                           monkeypatch):
    # 2h de sobrecarga: a vigília de STALL do owner NÃO pode escalar "captura ESTAGNADA"
    # curso a curso (o portão segurou o disparo; ninguém empacou) — só o aviso AGREGADO
    # vira essencial depois de ATHENA_CARGA_ALERTA_S, e com dedup.
    monkeypatch.setattr(athena_local, "_CARGA_ALERTA_S", 3600.0)
    ref = [_INCIDENTE]
    cursos, sp, alertas, voz, esp, estado, voo, estado_carga, ciclo = _loop(tmp_path, ref)
    t = 1000.0
    for _ in range(61):                                    # 2h, ciclo de 120s
        ciclo(t)
        t += 120.0
    assert sp.calls == []
    assert [p for p, _ in voz.escaladas if p.tipo == "captura_estagnada"] == []
    essenciais = [kw for _m, kw in alertas.carga if kw.get("essencial")]
    nao_ess = [kw for _m, kw in alertas.carga if not kw.get("essencial")]
    assert essenciais and nao_ess                          # começou só-log, escalou depois
    assert len({kw["chave"] for kw in essenciais}) == 1    # chave ÚNICA (o Alertas dedupa)
    esc = [r for r in esp.regs if r[2].get("tipo") == "escalada"]
    assert len(esc) == 1 and "sobrecarregada" in esc[0][0]  # 1 escalada por episódio
    # a sobrecarga passa: dispara (escalonado) e o episódio FECHA já no 1º ciclo aliviado
    # (o adiamento da rampa não é sobrecarga — não pode virar "sobrecarregada há 2 h")
    ref[0] = _NORMAL
    ciclo(t)
    assert len(sp.calls) == 2 and "desde" not in estado_carga
    ciclo(t + 120.0)
    assert len(sp.calls) == 4


def test_sem_portao_o_loop_segue_identico(tmp_path):
    # retrocompat: LocalExecutor sem portão (o default do construtor) => dispara como
    # sempre, sem ler sensor nenhum.
    cursos = [captura.CursoLocal(u, f"conta-{i}", p, total_esperado=18) for i, (u, p) in
              enumerate([(HOT, "hotmart"), (MK, "memberkit"), (KIW, "kiwify"),
                         (HUB, "hubla")])]
    sp = _SpawnTee([""] * 4)
    ex = _executor(tmp_path, cursos, sp)
    assert ex._portao_carga is None
    athena_local.ciclo_local(cursos, ex, lambda c: (0, 18), _Voz(), {}, {},
                             agora=1000.0, disjuntor=disjuntor)
    assert len(sp.calls) == 4


def test_alertas_real_maquina_sobrecarregada_so_loga_ou_pinga_com_dedup():
    from maestro.alertas import Alertas

    class _TG:
        def __init__(self):
            self.msgs = []

        def send_message(self, chat, texto):
            self.msgs.append(texto)

    tg = _TG()
    a = Alertas(tg, [1], nivel="essencial", dedup_janela_s=3600, relogio=lambda: 1000.0)
    a.maquina_sobrecarregada("x", essencial=False, chave=("carga",))
    assert tg.msgs == []                                   # auto-tratado: só-log
    a.maquina_sobrecarregada("x", essencial=True, chave=("carga",))
    a.maquina_sobrecarregada("x", essencial=True, chave=("carga",))
    assert len(tg.msgs) == 1 and "ADIADOS" in tg.msgs[0]   # essencial + dedup
