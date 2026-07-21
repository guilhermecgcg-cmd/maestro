"""HARNESS DE EVIDÊNCIA — P3 vigia+causa com SUBPROCESSOS REAIS (não dublês).

Prova o critério de pronto:
  1) um filho REAL que morre imprimindo 'session expired' -> Obito -> escalar_reseed;
  2) um kill -9 REAL (returncode -9) -> relancar;
  3) um erro INVENTADO -> UMA chamada `claude -p` REAL cuja saída cai no conjunto fechado;
  4) um arquivo de autópsia REAL em ~/.athena-local/autopsias/ com exit_code+stderr_tail.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from maestro import vigia, causa


def _escrever_lock(lock_dir, conta, curso, pid):
    slug = hashlib.sha256(str(conta).encode()).hexdigest()[:16]
    p = os.path.join(lock_dir, slug + ".lock")
    with open(p, "w") as f:
        json.dump({"pid": pid, "course_url": curso, "conta": conta}, f)
    return p


def _morre_com(msg, exit_code):
    """Spawn REAL: um python que escreve `msg` no stderr e sai com `exit_code`."""
    errf = tempfile.NamedTemporaryFile("w+", suffix=".err", delete=False)
    errf.close()
    code = "import sys; sys.stderr.write(%r); sys.exit(%d)" % (msg, exit_code)
    with open(errf.name, "w") as fh:
        proc = subprocess.Popen([sys.executable, "-c", code], stderr=fh)
    proc.wait()
    return proc, errf.name


def caso1_sessao(real_aut_dir):
    print("\n== CASO 1: filho REAL morre com 'session expired' ==")
    with tempfile.TemporaryDirectory() as d:
        lock_dir = os.path.join(d, "locks"); os.makedirs(lock_dir)
        proc, errf = _morre_com("Traceback...\nSessionLostError: session expired\n", 1)
        print("  pid=%d  returncode=%d" % (proc.pid, proc.returncode))
        _escrever_lock(lock_dir, "hotmart-a", "https://hotmart/curso-x", proc.pid)
        fonte = vigia.FonteFilho(exit_code=proc.returncode, stderr_path=errf, pid=proc.pid)
        # ESCREVE a autópsia no diretório REAL ~/.athena-local/autopsias/
        obitos = vigia.autopsia(lock_dir, {"hotmart-a": fonte}, autopsia_dir=real_aut_dir)
        assert len(obitos) == 1, obitos
        o = obitos[0]
        print("  Obito: conta=%s curso=%s exit_code=%s flaps=%d" % (
            o.conta, o.curso, o.exit_code, o.flaps_na_janela))
        print("  stderr_tail=%r" % o.stderr_tail)
        d1 = causa.classificar(o)
        print("  Decisao: %s (fonte=%s, motivo=%s)" % (d1.acao, d1.fonte, d1.motivo))
        assert o.exit_code == 1
        assert "session expired" in o.stderr_tail
        assert d1.acao == "escalar_reseed", d1
        os.unlink(errf)
        return o


def caso2_sigkill():
    print("\n== CASO 2: filho REAL levado por kill -9 ==")
    with tempfile.TemporaryDirectory() as d:
        lock_dir = os.path.join(d, "locks"); os.makedirs(lock_dir)
        aut = os.path.join(d, "aut")
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        time.sleep(0.3)
        os.kill(proc.pid, 9)                      # SIGKILL REAL
        proc.wait()
        print("  pid=%d  returncode=%d (esperado -9)" % (proc.pid, proc.returncode))
        _escrever_lock(lock_dir, "kajabi-b", "https://kajabi/curso-y", proc.pid)
        fonte = vigia.FonteFilho(exit_code=proc.returncode, pid=proc.pid)
        obitos = vigia.autopsia(lock_dir, {"kajabi-b": fonte}, autopsia_dir=aut)
        assert len(obitos) == 1
        d2 = causa.classificar(obitos[0])
        print("  Obito: exit_code=%s  Decisao: %s (fonte=%s)" % (
            obitos[0].exit_code, d2.acao, d2.fonte))
        assert obitos[0].exit_code == -9
        assert d2.acao == "relancar", d2


def caso3_claude_p():
    print("\n== CASO 3: erro INVENTADO -> UMA chamada `claude -p` REAL ==")
    with tempfile.TemporaryDirectory() as d:
        lock_dir = os.path.join(d, "locks"); os.makedirs(lock_dir)
        aut = os.path.join(d, "aut")
        msg = ("QuuxDriverPanic: flarn coefficient 0x9F drifted past the "
               "zorble threshold (code BLERG-42) while quantizing the widget\n")
        proc, errf = _morre_com(msg, 3)
        _escrever_lock(lock_dir, "stoa-c", "https://stoa/curso-z", proc.pid)
        fonte = vigia.FonteFilho(exit_code=proc.returncode, stderr_path=errf, pid=proc.pid)
        obitos = vigia.autopsia(lock_dir, {"stoa-c": fonte}, autopsia_dir=aut)
        o = obitos[0]
        # confirma que é DESCONHECIDA (determinístico não classifica)
        det = causa._deterministico(o)
        print("  determinístico(obito) = %r (None => vai ao LLM)" % (det,))
        assert det is None, "esperava desconhecida, veio %r" % (det,)

        transcript = {"n": 0}
        def llm_contado(prompt):
            transcript["n"] += 1
            transcript["prompt"] = prompt
            out = causa.seam_claude_p(prompt)     # `claude -p` REAL
            transcript["resposta"] = out
            return out

        d3 = causa.classificar(o, llm=llm_contado)
        print("  chamadas ao claude -p: %d" % transcript["n"])
        print("  --- PROMPT enviado ao claude -p (%d chars) ---" % len(transcript["prompt"]))
        print(transcript["prompt"])
        print("  --- RESPOSTA do claude -p ---")
        print(repr(transcript["resposta"]))
        print("  Decisao: %s (fonte=%s)" % (d3.acao, d3.fonte))
        assert transcript["n"] == 1, "esperava EXATAMENTE 1 chamada"
        assert d3.acao in causa.ACOES
        assert d3.fonte == "llm", d3           # veio do LLM, não fail-closed
        os.unlink(errf)


def main():
    real_aut_dir = os.path.join(os.path.expanduser("~"), ".athena-local", "autopsias")
    o1 = caso1_sessao(real_aut_dir)
    caso2_sigkill()
    caso3_claude_p()

    print("\n== CASO 4: arquivo de autópsia REAL em ~/.athena-local/autopsias/ ==")
    # acha o arquivo recém-gravado desta conta
    import glob
    cands = sorted(glob.glob(os.path.join(real_aut_dir, "*-hotmart-a.json")))
    assert cands, "nenhuma autópsia gravada em %s" % real_aut_dir
    ultimo = cands[-1]
    with open(ultimo) as f:
        rec = json.load(f)
    print("  arquivo: %s" % ultimo)
    print("  conteúdo: %s" % json.dumps(rec, ensure_ascii=False))
    assert rec["exit_code"] == 1
    assert "session expired" in rec["stderr_tail"]
    assert rec["detectado_por"] == "exit_code"
    print("\nTODOS OS CASOS PASSARAM.")


if __name__ == "__main__":
    main()
