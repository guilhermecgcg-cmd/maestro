"""F4-a: DENTES da ESPINHA fiada no loop doméstico (call-sites E1–E14).

A espinha (`decisoes.registrar_decisao`) deixa de ter 0 call-sites: cada TRANSIÇÃO
de estado do loop registra a decisão autônoma (e o custo, separado medido/presumido).
Três invariantes com dentes:

  1. Um ciclo com fixture REGISTRA a decisão (E10 disparo) e o custo (E14 claude -p).
     Remover um call-site faz o assert do `o_que` correspondente FALHAR.
  2. A espinha REAL (`maestro.decisoes`, DIR_PADRAO redirecionado) grava o JSONL do
     dia — o log deixa de ser vazio (o baseline a vencer).
  3. Uma espinha que LEVANTA em `registrar_decisao` NÃO derruba o loop (o wrapper
     `_registrar` é fail-safe, como pulso/batimento/alertas) — o null-object garante
     o default; o wrapper garante que observabilidade nunca mata o loop.
"""
import asyncio

from maestro import athena_local, causa, decisoes, vigia
from maestro.adaptadores import captura

C1 = "https://hotmart.com/pt-br/x/products/111"


class FakeVoz:
    def __init__(self):
        self.avisos = []
        self.escaladas = []

    def avisar_acao(self, acao):
        self.avisos.append(acao)

    def escalar(self, problema, pedido):
        self.escaladas.append((problema, pedido))

    def _enviar(self, texto):
        self.escaladas.append(("_enviar", texto))


class FakeExecutorObitos:
    def __init__(self, conta_de):
        self._conta_de = conta_de
        self.ativos = set()
        self.disparos = []
        self._obitos = {}

    def curso_ativo(self, curso):
        return curso in self.ativos

    def disparar(self, curso):
        conta = self._conta_de[curso]
        if curso in self.ativos:
            return f"ja:{curso}"
        for c in self.ativos:
            if self._conta_de[c] == conta:
                raise captura.ContaOcupada(f"conta {conta} ocupada")
        self.ativos.add(curso)
        self.disparos.append(curso)
        return f"local_iniciada:{curso}"

    def matar(self, curso, *, exit_code=None, stderr=None):
        self.ativos.discard(curso)
        conta = self._conta_de[curso]
        self._obitos[conta] = {"conta": conta, "curso": curso, "exit_code": exit_code,
                               "stderr_tail": stderr, "pid": None}

    def drenar_obitos(self):
        out = dict(self._obitos)
        self._obitos = {}
        return out


class SpyEspinha:
    def __init__(self):
        self.regs = []

    def registrar_decisao(self, o_que, por_que, **kw):
        self.regs.append({"o_que": o_que, "por_que": por_que, **kw})


class EspinhaQuebrada:
    """Uma espinha DEFEITUOSA: sempre levanta. O loop tem de sobreviver a ela."""
    def registrar_decisao(self, *a, **k):
        raise RuntimeError("espinha explodiu de propósito")


def _curso(url, conta="a", plataforma="hotmart", total=0):
    return captura.CursoLocal(url=url, conta=conta, plataforma=plataforma,
                              total_esperado=total)


def _prog(mapa):
    return lambda curso: mapa.get(curso, (0, 0))


def _reais(tmp_path):
    return dict(vigia=vigia, causa=causa,
                lock_dir=str(tmp_path / "locks"), autopsia_dir=str(tmp_path / "aut"))


# ==========================================================================
# 1. Ciclo registra DECISÃO (E10 disparo) e CUSTO (E14 claude -p, presumido)
# ==========================================================================
def test_espinha_registra_decisao_e_custo_do_ciclo(tmp_path):
    esp = SpyEspinha()
    voz = FakeVoz()
    ex = FakeExecutorObitos({C1: "a"})
    estado, voo = {}, {}
    cursos = [_curso(C1, "a", "hotmart", 18)]
    prog = _prog({C1: (0, 18)})

    # ciclo 1: dispara (E10).
    athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=1000.0,
                             espinha=esp, **_reais(tmp_path))
    assert C1 in ex.disparos
    decisoes_e10 = [r for r in esp.regs if "disparei captura" in r["o_que"]]
    assert decisoes_e10, "E10: disparo não foi registrado na espinha"
    assert decisoes_e10[-1]["reversivel"] is True

    # ciclo 2: o filho MORRE de causa DESCONHECIDA; com llm ligado, a classificação
    # usa `claude -p` (fonte='llm') → E14 grava o CUSTO presumido (medido=False).
    ex.matar(C1, exit_code=1, stderr="processo caiu com erro generico X")
    llm_fake = lambda prompt: '{"acao": "relancar"}'
    athena_local.ciclo_local(cursos, ex, prog, voz, voo, estado, agora=1100.0,
                             espinha=esp, llm=llm_fake, **_reais(tmp_path))

    custos = [r for r in esp.regs if r.get("tipo") == "custo"]
    assert custos, "E14: custo do claude -p não foi registrado"
    c = custos[-1]
    assert c["medido"] is False               # PRESUMIDO, nunca somado com medido
    assert c["custo_usd"] > 0
    assert c["modelo"] == "claude -p"
    # E4: a morte transitória (relancar) virou uma decisão de 'recozer e re-tentar'.
    assert any("recozer e re-tentar" in r["o_que"] for r in esp.regs), \
        "E4: decisão de recozimento não registrada"


# ==========================================================================
# 2. A espinha REAL grava o JSONL do dia (o log deixa de ser vazio)
# ==========================================================================
def test_espinha_real_grava_jsonl_do_dia(tmp_path, monkeypatch):
    # Redireciona o store da espinha real para um tmp (nada toca ~/.athena-local).
    monkeypatch.setattr(decisoes, "DIR_PADRAO", tmp_path / "decisoes")
    voz = FakeVoz()
    ex = FakeExecutorObitos({C1: "a"})
    cursos = [_curso(C1, "a", "hotmart", 18)]

    athena_local.ciclo_local(cursos, ex, _prog({C1: (0, 18)}), voz, {}, {},
                             agora=1000.0, espinha=decisoes, **_reais(tmp_path))

    resumo = decisoes.resumo_do_dia()          # lê do DIR_PADRAO redirecionado
    assert resumo["totais"]["n_decisoes"] >= 1  # o log NÃO é mais vazio (baseline vencido)
    assert any("disparei captura" in d["o_que"] for d in resumo["decisoes"])


# ==========================================================================
# 3. Espinha que LEVANTA não derruba o loop (fail-safe do _registrar)
# ==========================================================================
def test_espinha_quebrada_nao_derruba_ciclo(tmp_path):
    """Uma espinha cujo registrar_decisao SEMPRE levanta não pode impedir o disparo
    nem propagar exceção — observabilidade jamais mata o loop (dente do null-object
    + wrapper). Sem o `_registrar` fail-safe, o disparo (E10) estouraria aqui."""
    voz = FakeVoz()
    ex = FakeExecutorObitos({C1: "a"})
    cursos = [_curso(C1, "a", "hotmart", 18)]
    # NÃO levanta, mesmo com a espinha explodindo em cada call-site.
    athena_local.ciclo_local(cursos, ex, _prog({C1: (0, 18)}), voz, {}, {},
                             agora=1000.0, espinha=EspinhaQuebrada(), **_reais(tmp_path))
    assert C1 in ex.disparos                    # o loop fez o trabalho apesar da espinha


def test_espinha_quebrada_no_ciclo_estourado_nao_derruba_rodar(tmp_path):
    """E13 + fail-safe: um CONTROLE que levanta faz o ciclo estourar; a espinha
    quebrada no ramo de escalada (E13) também levanta — e AINDA ASSIM o loop não
    morre (rodar completa as iterações e escala honesto)."""
    class ControleQuebrado:
        def filtrar_cursos(self, cursos, path=None):
            raise RuntimeError("controle.yaml corrompido")

    voz = FakeVoz()
    ex = FakeExecutorObitos({C1: "a"})
    cursos = [_curso(C1, "a", "hotmart", 18)]

    async def _fake_sleep(_):
        return None

    n = asyncio.run(athena_local.rodar(
        cursos, ex, _prog({C1: (0, 18)}), voz, sleep=_fake_sleep, max_iters=2,
        controle=ControleQuebrado(), controle_path=str(tmp_path / "c.yaml"),
        espinha=EspinhaQuebrada(), **_reais(tmp_path)))

    assert n == 2                                # o loop NÃO morreu (rodou as 2 iters)
    assert any(p.tipo == "ciclo_local_estourou" for p, _ in voz.escaladas)
