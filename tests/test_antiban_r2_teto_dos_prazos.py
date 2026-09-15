"""RODADA 2, ITEM 3 — os prazos do exit 4 não passam do limite de leitura do estado em disco.

O ACHADO: `_carregar_estado_cursos` DESCARTA no reinício todo instante além de
`_ESTADO_FUTURO_MAX_S` (2 dias, a defesa contra relógio/arquivo corrompido), mas
ATHENA_BENCH_EXIT4_EXPIRA_S e ATHENA_ESPERA_EXIT4_S não tinham teto. Com a expiração do bench
configurada em 5 dias, o `benched_exit4_ate` gravado era jogado fora no primeiro reinício do
vigia externo e o curso benchado voltava a disparar na hora.

AGORA: as duas envs têm teto igual ao limite de leitura (WARNING acima dele).
"""
import os
import subprocess
import sys

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.parametrize("espera,expira,esperado", [
    ("999999", "432000", ["172800.0", "172800.0"]),
    ("172800", "172800", ["172800.0", "172800.0"]),
    ("600", "86400", ["600.0", "86400.0"]),
])
def test_espera_e_expiracao_do_exit4_tem_teto_no_limite_de_leitura(espera, expira, esperado):
    env = dict(os.environ, ATHENA_ESPERA_EXIT4_S=espera, ATHENA_BENCH_EXIT4_EXPIRA_S=expira)
    r = subprocess.run(
        [sys.executable, "-c", "from maestro import athena_local as a; "
         "print(a._ESPERA_EXIT4_S, a._BENCH_EXIT4_EXPIRA_S, a._ESTADO_FUTURO_MAX_S)"],
        cwd=RAIZ, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-1200:]
    *valores, limite = r.stdout.split()
    assert valores == esperado, (r.stdout, r.stderr[-600:])
    assert float(limite) == 172800.0
    if espera == "999999":
        assert "ATHENA_BENCH_EXIT4_EXPIRA_S" in r.stderr and "ATHENA_ESPERA_EXIT4_S" in r.stderr
