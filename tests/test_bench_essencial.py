"""BENCH exit-5 ESSENCIAL, com dedup por curso e SEM frase de sessão morta (achado r6).

ANTES: o alerta do bench (N sondas inconclusivas exit-5 seguidas no MESMO curso => curso
PARADO) era "não essencial" — só log. Num adaptador novo, um defeito PERMANENTE (a sonda
inconclusiva para sempre) ficava calado: o curso parava e ninguém sabia. E o texto dizia
"NÃO é sessão morta: sem reseed/relogin" — "sessão morta" e "relogin" casam a `_RE_SESSAO`
do classificador, a âncora que tem PRECEDÊNCIA sobre o exit code (reseed irredutível).

AGORA: essencial (Telegram) com chave ("bench", curso); texto que diz o que fazer (conferir
a URL no YAML / o adaptador) e NENHUMA frase de morte; o rótulo do curso passa pelo
`rotulo_seguro` (um slug/título da plataforma pode trazer um termo desses).

A conferência do texto usa o classificador REAL do caminho VIVO (maestro-athlocal), lido
em modo SÓ LEITURA a partir do fonte (sem importar do caminho — nada de .pyc lá).
"""
import os
import sys
import types
from types import SimpleNamespace

import pytest

from maestro import athena_local, causa
from maestro.adaptadores import captura
from maestro.alertas import Alertas
from maestro.rotulo import MASCARA, casa_ancora_de_morte, rotulo_seguro
from tests.test_athena_integracao import (FakeExecutorObitos, FakeVoz, _curso, _prog,
                                          _reais)

_CAUSA_VIVA = "/Users/guilhermerodrigues/teste/maestro-athlocal/maestro/causa.py"
_STDERR_EXIT5 = ("SONDA DE SESSÃO INCONCLUSIVA (transitório de rede/timeout): "
                 "probe /v1/navigation timeout")
C1 = "https://hotmart.com/pt-br/x/products/111"
C2 = "https://hotmart.com/pt-br/y/products/222"


def _causa_viva():
    """O `maestro/causa.py` que o daemon VIVO roda, carregado do FONTE (open + exec num
    módulo novo): só leitura, nenhum byte escrito no caminho vivo (sem __pycache__)."""
    if not os.path.exists(_CAUSA_VIVA):
        pytest.skip("caminho vivo do maestro ausente nesta máquina")
    with open(_CAUSA_VIVA, encoding="utf-8") as f:
        fonte = f.read()
    nome = "_causa_viva_somente_leitura"
    mod = types.ModuleType(nome)
    mod.__file__ = _CAUSA_VIVA
    sys.modules[nome] = mod                      # dataclasses resolve o módulo por nome
    try:
        exec(compile(fonte, _CAUSA_VIVA, "exec"), mod.__dict__)
    finally:
        sys.modules.pop(nome, None)
    return mod


def _nao_declara_morte(viva, texto):
    """O texto NÃO casa as âncoras cruas de sessão/credencial do classificador vivo, e o
    classificador vivo (determinístico) não o toma por sessão morta nem token ruim."""
    assert not viva._RE_SESSAO.search(texto), (viva._RE_SESSAO.search(texto), texto)
    assert not viva._RE_TOKEN.search(texto), (viva._RE_TOKEN.search(texto), texto)
    for code in (1, 5, None):
        det = viva._deterministico(SimpleNamespace(stderr_tail=texto, exit_code=code,
                                                   curso=C1, conta="a"))
        assert det is None or det[0] not in ("escalar_reseed", "escalar_token"), (code, det)


class _TG:
    def __init__(self):
        self.msgs = []

    def send_message(self, chat, texto):
        self.msgs.append(texto)


def _benchar(tmp_path, alertas, cursos_contas, *, vezes=3, t0=1000.0):
    """3 mortes exit-5 idênticas em cada curso (contas distintas) -> bench de cada um."""
    voz = FakeVoz()
    ex = FakeExecutorObitos(dict(cursos_contas))
    estado, voo = {}, {}
    cursos = [_curso(u, c, "hotmart", 18) for u, c in cursos_contas]
    common = _reais(tmp_path, alertas)
    prog = _prog({u: (0, 18) for u, _c in cursos_contas})
    t = t0
    for _ in range(vezes):
        athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=t, **common)
        for u, _c in cursos_contas:
            ex.matar(u, exit_code=5, stderr=_STDERR_EXIT5)
        t += 1
    athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=t, **common)
    return ex, estado, cursos, prog, common, voz, voo, t


# ==========================================================================
# 1) ESSENCIAL + dedup por curso (Alertas REAL, Telegram dublê)
# ==========================================================================
def test_bench_exit5_pinga_telegram_essencial_uma_vez_por_curso(tmp_path):
    tg = _TG()
    ex, estado, *_ = _benchar(tmp_path, Alertas(tg, [1]), [(C1, "a"), (C2, "b")])
    assert estado[C1].get("benched_exit5") and estado[C2].get("benched_exit5")
    bench = [m for m in tg.msgs if "BENCH exit-5" in m]
    # DENTES: antes -> [] (o bench era só-log e o curso parava calado)
    assert len(bench) == 2, tg.msgs                        # 1 por curso
    assert any(C1 in m for m in bench) and any(C2 in m for m in bench)
    assert all("confira a URL" in m for m in bench)        # diz o que fazer
    assert not any("expirou" in m for m in tg.msgs)        # jamais o alerta de reseed


def test_bench_do_mesmo_curso_na_janela_e_dedupado(tmp_path):
    # desbench (avanço real no Notion) e re-bench dentro da janela: 1 ping só.
    tg = _TG()
    alertas = Alertas(tg, [1], dedup_janela_s=3600)
    ex, estado, cursos, prog, common, voz, voo, t = _benchar(tmp_path, alertas, [(C1, "a")])
    assert len([m for m in tg.msgs if "BENCH" in m]) == 1
    avancou = _prog({C1: (5, 18)})
    athena_local.ciclo_local(cursos, ex, avancou, voz, voo, estado, agora=t + 1, **common)
    assert not estado[C1].get("benched_exit5")             # desbenchou
    t += 2
    for _ in range(12):
        if ex.curso_ativo(C1):
            ex.matar(C1, exit_code=5, stderr=_STDERR_EXIT5)
        athena_local.ciclo_local(cursos, ex, avancou, voz, voo, estado, agora=t, **common)
        if estado[C1].get("benched_exit5"):
            break
        t += 7 * 86400.0                                   # o backoff expira
    assert estado[C1].get("benched_exit5")                 # re-benchou
    assert len([m for m in tg.msgs if "BENCH" in m]) == 1  # dedup por curso na janela


def test_bench_chama_o_alerta_essencial_com_chave_por_curso():
    class _Spy:
        def __init__(self):
            self.chamadas = []

        def captura_morreu(self, plataforma, motivo, **kw):
            self.chamadas.append((plataforma, motivo, kw))

    from maestro.vigia import Obito

    class _Disj:
        def registrar_falha(self, st, agora):
            return None

    spy = _Spy()
    st = {"exit5_seguidas": 2}
    ob = Obito(conta="a", curso=C1, exit_code=5, stderr_tail=_STDERR_EXIT5,
               flaps_na_janela=1, ts="")
    athena_local._aplicar_decisao(C1, st, ob, SimpleNamespace(acao="relancar", motivo="",
                                                              fonte="deterministico"),
                                  disjuntor=_Disj(), alertas=spy, agora=1.0,
                                  meta_por_curso=None, flap_min=99)
    [(_plat, motivo, kw)] = spy.chamadas
    assert kw == {"essencial": True, "chave": ("bench", C1)}
    assert st["benched_exit5"] is True and st["irredutivel"] is True


# ==========================================================================
# 2) NENHUMA frase de morte — conferido contra o classificador VIVO
# ==========================================================================
def test_texto_do_bench_nao_casa_as_ancoras_de_morte_do_classificador_vivo(tmp_path):
    viva = _causa_viva()
    tg = _TG()
    _benchar(tmp_path, Alertas(tg, [1]), [(C1, "a")])
    [msg] = [m for m in tg.msgs if "BENCH" in m]           # o texto INTEIRO do Telegram
    _nao_declara_morte(viva, msg)


def test_url_hostil_da_plataforma_e_mascarada_e_o_curso_segue_reconhecivel(tmp_path):
    viva = _causa_viva()
    hostis = ["https://dashboard.kiwify.com.br/course/re-login-avancado",
              "https://x.memberkit.com.br/curso/Unauthorized-Access-101",
              "https://app.hub.la/user_groups/forbidden-secrets"]
    tg = _TG()
    _benchar(tmp_path, Alertas(tg, [1]), [(u, f"c{i}") for i, u in enumerate(hostis)])
    bench = [m for m in tg.msgs if "BENCH" in m]
    assert len(bench) == 3
    for m in bench:
        _nao_declara_morte(viva, m)                        # DENTES: sem o rótulo, casava
        assert MASCARA in m
    assert any("-avancado" in m and "kiwify" in m for m in bench)  # ainda dá p/ achar


def test_regex_do_rotulo_sao_as_do_classificador_vivo():
    # o `rotulo_seguro` importa as âncoras de maestro.causa DESTE código; se o caminho vivo
    # ganhar uma âncora que este não tem, o rótulo deixaria passar — este teste avisa.
    viva = _causa_viva()
    assert viva._RE_SESSAO.pattern == causa._RE_SESSAO.pattern
    assert viva._RE_TOKEN.pattern == causa._RE_TOKEN.pattern


# ==========================================================================
# 3) rotulo_seguro (unidade)
# ==========================================================================
@pytest.mark.parametrize("texto", [
    "Aula 3 — Sessão expirada? Como resolver",
    "Módulo: refaça o login no app",
    "Unauthorized Access 101",
    "https://x.com/c/relogin",
    "Session expired: case study",
    "Invalid API key handling",
])
def test_rotulo_seguro_mascara_termo_sensivel(texto):
    r = rotulo_seguro(texto)
    assert not casa_ancora_de_morte(r), r
    assert MASCARA in r


def test_rotulo_seguro_deixa_passar_o_normal_e_nunca_levanta():
    assert rotulo_seguro(C1) == C1
    assert rotulo_seguro("  Curso   de   Python ") == "Curso de Python"
    assert rotulo_seguro(None) == "«sem nome»"
    assert rotulo_seguro(12345) == "12345"
    longo = "a" * 500
    assert len(rotulo_seguro(longo)) == 200
    assert "sessão viva" == rotulo_seguro("sessão viva")   # benigno não é mascarado


def test_textos_novos_do_portao_e_da_autopsia_nao_casam_ancoras_do_classificador_vivo(tmp_path):
    # Tudo o que esta rodada passou a escrever (motivos do portão com histerese, da rampa,
    # a recusa por autópsia pendente, a descrição do portão) fica longe das âncoras.
    from maestro import carga
    from tests.test_evidencia_reap import _SpawnTee, _executor
    viva = _causa_viva()
    textos = []
    rel = [0.0]
    p = carga.PortaoCarga(sensor=lambda: carga.LeituraCarga(24.0, 8, 4.0),
                          relogio=lambda: rel[0], disparos_por_ciclo=1)
    textos.append(p.descrever())
    textos.append(p.decidir(0).motivo)                     # sobrecarga (com a saída)
    p._sensor = lambda: carga.LeituraCarga(1.0, 8, 60.0)
    p.decidir(0)
    p.registrar_disparo()
    textos.append(p.decidir(1).motivo)                     # rampa
    p2 = carga.PortaoCarga(sensor=lambda: carga.LeituraCarga(1.0, 8, 60.0), max_motores=1)
    textos.append(p2.decidir(1).motivo)                    # teto
    sp = _SpawnTee([""])
    ex = _executor(tmp_path, [captura.CursoLocal(C1, "a", "hotmart")], sp)
    ex.disparar(C1)
    sp.calls[0]["proc"].encerrar(3)
    with pytest.raises(captura.AguardaAutopsia) as e:
        ex.disparar(C1)
    textos.append(str(e.value))
    for t in textos:
        _nao_declara_morte(viva, t)
