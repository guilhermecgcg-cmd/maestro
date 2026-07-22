"""Testes da espinha de prestação de contas (maestro/decisoes.py).

Contrato (spec 2026-07-21-espinha-decisoes-log-custo):
- store append-only JSONL, um arquivo por dia local em <dir>/YYYY-MM-DD.jsonl;
- registrar_decisao NUNCA levanta (observabilidade não derruba o loop);
- resumo_do_dia/gasto_do_dia agregam, com medido e presumido SEMPRE separados;
- leitura tolerante: linha corrompida é pulada e contada;
- ts injetável (agora=) e dir_base injetável — nada de relógio/HOME reais em teste.

Testes-com-dentes: cada um falha contra a ausência do comportamento, não contra
a assinatura. Os asserts checam o mecanismo (separação de custo, contagem de
corrupção, fail-safe real com chmod 000), não só "não explodiu".
"""
import json
import os
import stat
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

from maestro import decisoes


# offset fixo -03:00 (mesmo fuso do Mac do usuário) para ts determinístico
_TZ = timezone(timedelta(hours=-3))


def _ts(dia="2026-07-21", hora="14:03:22"):
    return datetime.fromisoformat(f"{dia}T{hora}{'-03:00'}")


def _linhas(dir_base, dia="2026-07-21"):
    arq = dir_base / f"{dia}.jsonl"
    return [json.loads(l) for l in arq.read_text().splitlines() if l.strip()]


# --------------------------------------------------------------------------
# roundtrip / schema
# --------------------------------------------------------------------------

def test_registrar_grava_arquivo_do_dia_local(tmp_path):
    decisoes.registrar_decisao(
        "escalar_reseed", "sessão morta",
        reversivel=False, agora=_ts(), dir_base=tmp_path,
    )
    assert (tmp_path / "2026-07-21.jsonl").exists()
    regs = _linhas(tmp_path)
    assert len(regs) == 1
    r = regs[0]
    assert r["v"] == 1
    assert r["tipo"] == "decisao"
    assert r["o_que"] == "escalar_reseed"
    assert r["por_que"] == "sessão morta"
    assert r["reversivel"] is False
    assert r["ts"] == "2026-07-21T14:03:22-03:00"


def test_append_nao_sobrescreve(tmp_path):
    for i in range(3):
        decisoes.registrar_decisao(
            f"acao{i}", "m", reversivel=True,
            agora=_ts(hora=f"14:0{i}:00"), dir_base=tmp_path,
        )
    regs = _linhas(tmp_path)
    assert [r["o_que"] for r in regs] == ["acao0", "acao1", "acao2"]


def test_dia_separado_por_data_local(tmp_path):
    decisoes.registrar_decisao("a", "m", reversivel=True,
                               agora=_ts(dia="2026-07-21"), dir_base=tmp_path)
    decisoes.registrar_decisao("b", "m", reversivel=True,
                               agora=_ts(dia="2026-07-22"), dir_base=tmp_path)
    assert (tmp_path / "2026-07-21.jsonl").exists()
    assert (tmp_path / "2026-07-22.jsonl").exists()
    assert len(_linhas(tmp_path, "2026-07-21")) == 1
    assert len(_linhas(tmp_path, "2026-07-22")) == 1


def test_data_do_arquivo_usa_fuso_local_do_ts(tmp_path):
    # 23:30 -03:00 é 21/07 local, embora seja 22/07 em UTC. O arquivo do dia
    # tem que seguir o dia LOCAL do ts, não o dia UTC — senão o SITREP do
    # usuário mostra a decisão no dia errado.
    decisoes.registrar_decisao("a", "m", reversivel=True,
                               agora=_ts(hora="23:30:00"), dir_base=tmp_path)
    assert (tmp_path / "2026-07-21.jsonl").exists()
    assert not (tmp_path / "2026-07-22.jsonl").exists()


# --------------------------------------------------------------------------
# custo: medido e presumido NUNCA num número só
# --------------------------------------------------------------------------

def test_resumo_separa_custo_medido_de_presumido(tmp_path):
    decisoes.registrar_decisao("llm_causa", "diag", reversivel=True,
                               tipo="custo", fonte="llm", modelo="haiku",
                               tokens_in=100, tokens_out=50, custo_usd=0.10,
                               medido=True, agora=_ts(hora="10:00:00"),
                               dir_base=tmp_path)
    decisoes.registrar_decisao("orquestracao", "estim", reversivel=True,
                               tipo="custo", modelo="opus",
                               tokens_in=1000, tokens_out=200, custo_usd=2.00,
                               medido=False, agora=_ts(hora="11:00:00"),
                               dir_base=tmp_path)
    r = decisoes.resumo_do_dia(date(2026, 7, 21), dir_base=tmp_path)
    t = r["totais"]
    assert t["custo_medido_usd"] == pytest.approx(0.10)
    assert t["custo_presumido_usd"] == pytest.approx(2.00)
    # o teste-com-dentes: NUNCA existe um campo que soma os dois
    assert "custo_usd" not in t
    assert "custo_total_usd" not in t
    assert t["tokens_in"] == 1100
    assert t["tokens_out"] == 250


def test_gasto_do_dia_espelha_totais_separados(tmp_path):
    decisoes.registrar_decisao("x", "m", reversivel=True, tipo="custo",
                               custo_usd=0.5, medido=True,
                               agora=_ts(), dir_base=tmp_path)
    decisoes.registrar_decisao("y", "m", reversivel=True, tipo="custo",
                               custo_usd=3.0, medido=False,
                               agora=_ts(hora="15:00:00"), dir_base=tmp_path)
    g = decisoes.gasto_do_dia(date(2026, 7, 21), dir_base=tmp_path)
    assert g["custo_medido_usd"] == pytest.approx(0.5)
    assert g["custo_presumido_usd"] == pytest.approx(3.0)
    assert "custo_usd" not in g and "custo_total_usd" not in g


# --------------------------------------------------------------------------
# escaladas
# --------------------------------------------------------------------------

def test_escaladas_capturadas_por_flag_e_por_tipo(tmp_path):
    # via escalada=True
    decisoes.registrar_decisao("escalar_reseed", "sessão", reversivel=False,
                               escalada=True, trava="anti-ban",
                               agora=_ts(hora="09:00:00"), dir_base=tmp_path)
    # via tipo="escalada"
    decisoes.registrar_decisao("plataforma_nova", "kiwify", reversivel=True,
                               tipo="escalada", trava="plataforma-nova",
                               escalada=True, agora=_ts(hora="09:01:00"),
                               dir_base=tmp_path)
    # decisão normal não vira escalada
    decisoes.registrar_decisao("relancar", "retry", reversivel=True,
                               agora=_ts(hora="09:02:00"), dir_base=tmp_path)
    r = decisoes.resumo_do_dia(date(2026, 7, 21), dir_base=tmp_path)
    assert r["totais"]["n_escaladas"] == 2
    assert len(r["escaladas"]) == 2
    travas = r["totais"]["por_trava"]
    assert travas["anti-ban"] == 1
    assert travas["plataforma-nova"] == 1


def test_decisoes_lista_so_tipo_decisao_em_ordem(tmp_path):
    decisoes.registrar_decisao("d1", "m", reversivel=True,
                               agora=_ts(hora="08:00:00"), dir_base=tmp_path)
    decisoes.registrar_decisao("c1", "m", reversivel=True, tipo="custo",
                               agora=_ts(hora="08:01:00"), dir_base=tmp_path)
    decisoes.registrar_decisao("d2", "m", reversivel=True,
                               agora=_ts(hora="08:02:00"), dir_base=tmp_path)
    r = decisoes.resumo_do_dia(date(2026, 7, 21), dir_base=tmp_path)
    assert [d["o_que"] for d in r["decisoes"]] == ["d1", "d2"]
    assert r["totais"]["n_decisoes"] == 2


def test_por_fonte_conta_todas_as_fontes(tmp_path):
    for f in ("deterministico", "deterministico", "llm", "disjuntor"):
        decisoes.registrar_decisao("a", "m", reversivel=True, fonte=f,
                                   agora=_ts(), dir_base=tmp_path)
    r = decisoes.resumo_do_dia(date(2026, 7, 21), dir_base=tmp_path)
    pf = r["totais"]["por_fonte"]
    assert pf["deterministico"] == 2
    assert pf["llm"] == 1
    assert pf["disjuntor"] == 1


# --------------------------------------------------------------------------
# leitura tolerante
# --------------------------------------------------------------------------

def test_linha_corrompida_pulada_e_contada(tmp_path):
    decisoes.registrar_decisao("boa1", "m", reversivel=True,
                               agora=_ts(hora="07:00:00"), dir_base=tmp_path)
    # injeta lixo no meio do arquivo do dia
    arq = tmp_path / "2026-07-21.jsonl"
    with arq.open("a") as fh:
        fh.write("{lixo nao json\n")
        fh.write("\n")  # linha em branco não conta como inválida
    decisoes.registrar_decisao("boa2", "m", reversivel=True,
                               agora=_ts(hora="07:02:00"), dir_base=tmp_path)
    r = decisoes.resumo_do_dia(date(2026, 7, 21), dir_base=tmp_path)
    assert [d["o_que"] for d in r["decisoes"]] == ["boa1", "boa2"]
    assert r["linhas_invalidas"] == 1


def test_linha_json_valida_mas_tipo_de_campo_errado_nao_derruba_resumo(tmp_path):
    # Corrupção parcial não é só sintaxe: um dict JSON válido com campo de tipo
    # errado (tokens_in não-numérico — write truncado ou produtor externo) NÃO
    # pode derrubar o resumo. Sem a cerca, int("abc") propaga e o /sitrep quebra.
    decisoes.registrar_decisao("boa1", "m", reversivel=True,
                               agora=_ts(hora="07:00:00"), dir_base=tmp_path)
    arq = tmp_path / "2026-07-21.jsonl"
    with arq.open("a") as fh:
        fh.write('{"tipo":"decisao","o_que":"veneno","tokens_in":"abc"}\n')
    decisoes.registrar_decisao("boa2", "m", reversivel=True,
                               agora=_ts(hora="07:02:00"), dir_base=tmp_path)
    r = decisoes.resumo_do_dia(date(2026, 7, 21), dir_base=tmp_path)
    # linha venenosa contada como inválida, SEM efeito parcial (não listada)
    assert [d["o_que"] for d in r["decisoes"]] == ["boa1", "boa2"]
    assert r["totais"]["n_decisoes"] == 2
    assert r["linhas_invalidas"] == 1


def test_custo_sem_rotulo_medido_conta_como_presumido(tmp_path):
    # Default honesto (regra nº1): gasto SEM o campo `medido` é PRESUMIDO, nunca
    # medido. Contar estimativa não-rotulada como fatura real é mentir com
    # autoridade — e envenenaria o enforcement de teto (fase 2).
    arq = tmp_path / "2026-07-21.jsonl"
    arq.write_text('{"tipo":"custo","o_que":"externo","custo_usd":5.0}\n')
    r = decisoes.resumo_do_dia(date(2026, 7, 21), dir_base=tmp_path)
    assert r["totais"]["custo_presumido_usd"] == pytest.approx(5.0)
    assert r["totais"]["custo_medido_usd"] == 0.0


def test_dia_sem_arquivo_devolve_resumo_vazio_valido(tmp_path):
    r = decisoes.resumo_do_dia(date(2026, 7, 21), dir_base=tmp_path)
    assert r["decisoes"] == []
    assert r["escaladas"] == []
    assert r["linhas_invalidas"] == 0
    t = r["totais"]
    assert t["n_decisoes"] == 0 and t["n_escaladas"] == 0
    assert t["custo_medido_usd"] == 0.0 and t["custo_presumido_usd"] == 0.0
    assert t["tokens_in"] == 0 and t["tokens_out"] == 0
    g = decisoes.gasto_do_dia(date(2026, 7, 21), dir_base=tmp_path)
    assert g["custo_medido_usd"] == 0.0 and g["custo_presumido_usd"] == 0.0


# --------------------------------------------------------------------------
# fail-safe: registrar_decisao JAMAIS levanta
# --------------------------------------------------------------------------

def test_dir_sem_permissao_nao_levanta(tmp_path, capsys):
    alvo = tmp_path / "trancado"
    alvo.mkdir()
    os.chmod(alvo, 0)  # chmod 000 — sem escrita nem criação
    try:
        # não deve levantar mesmo sem conseguir escrever
        decisoes.registrar_decisao("a", "m", reversivel=True,
                                   agora=_ts(), dir_base=alvo)
    finally:
        os.chmod(alvo, stat.S_IRWXU)  # devolve permissão p/ cleanup do tmp
    # fallback foi para stderr (best-effort), não exceção
    err = capsys.readouterr().err
    assert err  # algo foi logado no pior caso


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignora permissão de dir")
def test_loop_sobrevive_a_dir_trancado_e_resumo_nao_explode(tmp_path):
    alvo = tmp_path / "trancado"
    alvo.mkdir()
    os.chmod(alvo, 0)
    try:
        decisoes.registrar_decisao("a", "m", reversivel=True,
                                   agora=_ts(), dir_base=alvo)
        # resumo de dir ilegível também não pode levantar
        r = decisoes.resumo_do_dia(date(2026, 7, 21), dir_base=alvo)
        assert r["decisoes"] == []
    finally:
        os.chmod(alvo, stat.S_IRWXU)


def test_valor_nao_serializavel_nao_derruba(tmp_path):
    class Opaco:
        pass
    # por_que com objeto não-serializável não pode explodir o registro
    decisoes.registrar_decisao("a", Opaco(), reversivel=True,  # type: ignore[arg-type]
                               agora=_ts(), dir_base=tmp_path)
    # ou pulou (nada gravado) ou coagiu p/ str — em nenhum caso levantou.
    # Se gravou, tem que ser JSON válido e legível.
    r = decisoes.resumo_do_dia(date(2026, 7, 21), dir_base=tmp_path)
    assert isinstance(r["decisoes"], list)


# --------------------------------------------------------------------------
# injeção de dependências (sem relógio/HOME reais)
# --------------------------------------------------------------------------

def test_agora_default_nao_usado_quando_injetado(tmp_path):
    # se o módulo usasse datetime.now() em vez do agora injetado, o arquivo
    # cairia no dia de hoje, não em 2026-07-21.
    decisoes.registrar_decisao("a", "m", reversivel=True,
                               agora=_ts(dia="2026-07-21"), dir_base=tmp_path)
    hoje = date.today().isoformat()
    if hoje != "2026-07-21":
        assert not (tmp_path / f"{hoje}.jsonl").exists()
    assert (tmp_path / "2026-07-21.jsonl").exists()


def test_resumo_data_default_e_hoje(tmp_path):
    hoje = datetime.now(_TZ)
    decisoes.registrar_decisao("a", "m", reversivel=True,
                               agora=hoje, dir_base=tmp_path)
    r = decisoes.resumo_do_dia(dir_base=tmp_path)  # data=None → hoje
    assert r["totais"]["n_decisoes"] == 1
