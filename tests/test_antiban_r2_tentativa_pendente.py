"""RODADA 2, ITEM 4 — a UMA tentativa pós-vencimento do bench conta como pendente até rodar.

O ACHADO: no vencimento do bench a passada gravava `exit4_ultimo = agora`, e a série decai em
ATHENA_BENCH_EXIT4_EXPIRA_S (24 h). Com a conta ocupada por outro curso por mais de 24 h, a
tentativa só rodava depois de a série decair: o abort dela virava série NOVA (1 de 3) em vez de
rebenchar na hora — e o curso ganhava mais duas idas à parede.

AGORA: o vencimento arma `exit4_rearmado`; a série não decai enquanto a tentativa não DISPARA
de fato, e o relógio do decaimento começa no disparo.
"""
from tests.test_antiban_exit4_classe_e_expiracao import DIA, H, _abortar
from tests.test_antiban_relancamento_exit4 import (_ABORT_EXIT4, IRMAO, T0, VIRAL, _Daemon,
                                                   _sem_tracker_vivo)  # noqa: F401 (autouse)

AMBOS = (VIRAL, IRMAO)                                    # a MESMA conta (tenant)


def test_a_UMA_tentativa_pos_vencimento_conta_como_pendente_ate_rodar(tmp_path):
    d = _Daemon(tmp_path)
    _abortar(d, _ABORT_EXIT4, 3)
    tb = T0 + 8 * H + 120                                  # ciclo que benchou
    assert d.st.get("benched_exit4") is True
    d.ciclo(tb + H, 100, cursos=AMBOS)                     # o irmão ocupa a conta e segue rodando
    assert d.disparos(IRMAO) == 1
    d.ciclo(tb + DIA + 60, 100, cursos=AMBOS)              # o bench vence; conta ocupada
    d.ciclo(tb + DIA + 26 * H, 100, cursos=AMBOS)          # mais de 24 h ocupada
    assert d.disparos() == 3
    d.ex.matar(IRMAO, exit_code=0, stderr="Stats: ok=1 audio=0 falhou=0 de 1")
    t = tb + DIA + 31 * H
    d.ciclo(t, 100, cursos=AMBOS)                          # conta livre: a tentativa roda agora
    assert d.disparos() == 4
    d.ex.matar(VIRAL, exit_code=4, stderr=_ABORT_EXIT4)
    d.ciclo(t + 120, 100, cursos=AMBOS)
    assert d.st.get("benched_exit4") is True, \
        "a conta ocupada por mais de 24 h transformou a UMA tentativa numa série nova"


def test_a_marca_persistida_vale_para_a_orfa_que_abortou_depois_de_um_reinicio(tmp_path):
    # a janela de crash: a tentativa disparou (o disparo consome a marca EM MEMÓRIA), mas o
    # loop morreu antes de gravar o estado do ciclo — o disco ainda tem a marca e o último
    # abort de 30 h atrás. A encarnação nova autopsia a órfã que abortou na parede: sem a
    # marca valendo no incremento, a série decairia e o curso ganharia mais duas idas.
    import json
    import os

    from maestro import athena_local, causa, disjuntor, vigia
    from tests.test_antiban_r2_morte_so_de_lock import (_CLI_PAREDE, PID_INEXISTENTE,
                                                        _ExecutorComErr)
    from tests.test_antiban_relancamento_exit4 import CONTA, _Alertas
    from tests.test_athena_integracao import FakeVoz, _curso, _prog

    agora = T0 + 40 * H
    estado = {VIRAL: {"disj_falhas": 3, "exit4_seguidas": 2, "exit4_ultimo": T0 + 10 * H,
                      "exit4_rearmado": 1, "ultimo_no_notion": 100}}
    locks = tmp_path / "locks"
    locks.mkdir()
    (locks / "orfa.lock").write_text(json.dumps(
        {"pid": PID_INEXISTENTE, "course_url": VIRAL, "conta": CONTA, "ts": agora}))
    ex = _ExecutorComErr({VIRAL: CONTA}, tmp_path / "err")
    os.makedirs(ex._err_dir)
    with open(ex._stderr_path(CONTA), "w", encoding="utf-8") as f:
        f.write(_CLI_PAREDE)
    athena_local.ciclo_local(
        [_curso(VIRAL, CONTA, "cademi", 300)], ex, _prog({VIRAL: (100, 300)}), FakeVoz(), {},
        estado, agora=agora, disjuntor=disjuntor, vigia=vigia, causa=causa, alertas=_Alertas(),
        lock_dir=str(locks), autopsia_dir=str(tmp_path / "aut"), boot_ts=1.0)
    assert estado[VIRAL].get("benched_exit4") is True, estado[VIRAL]


def test_depois_de_rodar_a_tentativa_o_decaimento_volta_a_valer(tmp_path):
    # never-stop: a tentativa rodou e o curso NÃO abortou; 30 h depois um abort isolado é série 1
    d = _Daemon(tmp_path)
    _abortar(d, _ABORT_EXIT4, 3)
    tb = T0 + 8 * H + 120
    d.ciclo(tb + DIA + 60, 100)                            # vence e dispara na hora
    assert d.disparos() == 4
    d.ex.matar(VIRAL, exit_code=-9, stderr="Killed")       # morte que não é abort do disjuntor
    d.ciclo(tb + DIA + 180, 100)
    d.ciclo(tb + DIA + 30 * H, 100)                        # relança mais de 24 h depois
    assert d.disparos() == 5
    d.ex.matar(VIRAL, exit_code=4, stderr=_ABORT_EXIT4)
    d.ciclo(tb + DIA + 30 * H + 120, 100)
    assert not d.st.get("benched_exit4") and d.st.get("exit4_seguidas") == 1
