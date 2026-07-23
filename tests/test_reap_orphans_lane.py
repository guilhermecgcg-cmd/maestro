"""HIGIENE DO DAEMON — REAPER DE ÓRFÃOS NO BOOT DA LANE (Camada A, process-based).

O motor (aula/motor/tracker.py) tem `reap_orphans` (Camada B): reset STALE de
LEGENDA_INFLIGHT por tempo. Ele NÃO toca os in-flight ASSÍNCRONOS
(transcrevendo*/capturando_nao_video) — a limpeza deles é de PROCESSO ("Camada A,
no runner da lane"), que nunca era feita: uma captura async que MORREU deixava a
aula presa em `transcrevendo`/`capturando_nao_video` para sempre (nenhum passe a
retoma se o dono do estado não roda). Este é o reaper que faltava: no BOOT da lane,
uma aula in-flight async cujo PROCESSO de captura NÃO está vivo volta a `pendente`.

DENTES: órfão SEM processo -> resetado; in-flight COM processo vivo -> INTOCADO
(nunca mexe numa captura viva — Ordem IV / anti-ban). Terminais e `pendente` nunca
são tocados. `notion_page_id`/`error` são PRESERVADOS (o resume arquiva a página
órfã antes de recriar — fail-closed, nunca `no_notion` de graça)."""
import sqlite3
import time

from maestro.adaptadores import captura

CURSO = "https://hotmart.com/pt-br/x/products/111"        # course_id = 111 (via /products/)
COURSE_ID = "111"


def _tracker(tmp_path):
    """Cria um {motor_dir}/tracker.db com o schema REAL do motor. Devolve o motor_dir."""
    motor_dir = str(tmp_path)
    con = sqlite3.connect(str(tmp_path / "tracker.db"))
    con.execute("""CREATE TABLE lessons(
        hash TEXT PRIMARY KEY, course_id TEXT, module TEXT, title TEXT,
        order_idx INTEGER, url TEXT, status TEXT DEFAULT 'pendente',
        notion_page_id TEXT, error TEXT, updated_at REAL)""")
    con.commit()
    con.close()
    return motor_dir


def _seed(motor_dir, rows):
    con = sqlite3.connect(f"{motor_dir}/tracker.db")
    for h, status, pid_page, err in rows:
        con.execute(
            "INSERT INTO lessons(hash,course_id,status,notion_page_id,error,updated_at) "
            "VALUES(?,?,?,?,?,?)", (h, COURSE_ID, status, pid_page, err, time.time()))
    con.commit()
    con.close()


def _status(motor_dir):
    con = sqlite3.connect(f"{motor_dir}/tracker.db")
    d = {h: (s, pg, er) for (h, s, pg, er) in
         con.execute("SELECT hash,status,notion_page_id,error FROM lessons")}
    con.close()
    return d


def test_reaper_reseta_async_inflight_sem_processo(tmp_path):
    md = _tracker(tmp_path)
    _seed(md, [
        ("a", "transcrevendo", "pg-a", "[falha 1/3]"),     # órfão áudio
        ("b", "capturando_nao_video", "pg-b", None),       # órfão não-vídeo
        ("c", "transcrevendo_embed", None, None),          # órfão embed
        ("d", "transcrevendo_youtube", None, None),        # órfão youtube
    ])
    n = captura.reap_orphans_local(CURSO, md, curso_ativo=lambda u: False)
    assert n == 4                                          # todos os 4 órfãos resetados
    st = _status(md)
    for h in ("a", "b", "c", "d"):
        assert st[h][0] == "pendente"                      # DENTE: reset -> pendente
    assert st["a"][1] == "pg-a"                            # notion_page_id PRESERVADO
    assert st["a"][2] == "[falha 1/3]"                     # error PRESERVADO (resume)


def test_reaper_nao_toca_captura_com_processo_vivo(tmp_path):
    md = _tracker(tmp_path)
    _seed(md, [("a", "transcrevendo", "pg-a", None)])
    n = captura.reap_orphans_local(CURSO, md, curso_ativo=lambda u: True)
    assert n == 0                                          # DENTE: processo vivo -> intocado
    assert _status(md)["a"][0] == "transcrevendo"


def test_reaper_nao_toca_terminais_nem_pendente(tmp_path):
    md = _tracker(tmp_path)
    _seed(md, [
        ("ok", "no_notion", "pg", None),                   # sucesso terminal
        ("sc", "sem_conteudo", None, None),                # terminal benigno
        ("fa", "falhou", "pg2", "x"),                      # falha terminal
        ("pe", "pendente", None, None),                    # já pendente
    ])
    n = captura.reap_orphans_local(CURSO, md, curso_ativo=lambda u: False)
    assert n == 0                                          # nada in-flight async -> 0
    st = _status(md)
    assert st["ok"][0] == "no_notion" and st["sc"][0] == "sem_conteudo"
    assert st["fa"][0] == "falhou" and st["pe"][0] == "pendente"


def test_reaper_fail_open_sem_db(tmp_path):
    # sem tracker.db (curso nunca capturado local) -> no-op, nunca estoura.
    assert captura.reap_orphans_local(CURSO, str(tmp_path), curso_ativo=lambda u: False) == 0


def test_reaper_url_sem_course_id_e_noop(tmp_path):
    md = _tracker(tmp_path)
    _seed(md, [("a", "transcrevendo", None, None)])
    # URL sem /products/<id> -> course_id não resolve -> no-op (fail-open), não toca nada.
    assert captura.reap_orphans_local("https://x/sem-id", md, curso_ativo=lambda u: False) == 0
    assert _status(md)["a"][0] == "transcrevendo"
