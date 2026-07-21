"""DRY-RUN da INTEGRAÇÃO — prova com EVIDÊNCIA (log real) que o loop doméstico USA as 6
partes. NÃO toca no daemon nem nas capturas vivas: usa um executor DUBLÊ (sem browser),
os MÓDULOS REAIS (controle/disjuntor/vigia/causa/batimento/alertas) e arquivos num tmpdir.

Cenas:
  (a) curso PAUSADO via controle.yaml NÃO dispara;
  (b) curso MORTO -> autópsia -> causa -> decisão do disjuntor (transitório re-tenta;
      sessão morta = irredutível, escala, NÃO martela);
  (c) BATIMENTO emite (mesmo MUDO, LOGA a linha que iria pro Telegram).

Rode: ATHENA vazia -> `python -m scripts.dry_run_integracao` (ou via .venv).
"""
import asyncio
import logging
import os
import sys
import tempfile

# Athena MUDA (fail-safe): nenhum POST ao Telegram — a evidência é o LOG local.
os.environ["TELEGRAM_BOT_TOKEN"] = "MUTED"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from maestro import athena_local, batimento, causa, controle, disjuntor, vigia  # noqa: E402
from maestro.adaptadores import captura  # noqa: E402
from maestro.alertas import Alertas  # noqa: E402

logging.basicConfig(level=logging.INFO, format="    LOG[%(name)s] %(message)s",
                    stream=sys.stdout)

C_HOT = "https://hotmart.com/pt-br/curso-vivo/products/111"
C_MK = "https://minha.memberkit.com.br/curso-pausado/9"


def cena(titulo):
    print("\n" + "=" * 78 + f"\n### {titulo}\n" + "=" * 78)


class VozLog:
    """Voz-dublê: imprime cada aviso/escalada (o que o operador veria no Telegram)."""
    def avisar_acao(self, acao):
        if getattr(acao, "executada", False):
            print(f"    VOZ 🔧 {acao.descricao}")

    def escalar(self, problema, pedido):
        print(f"    VOZ ⚠️  [{problema.tipo}] {pedido}")

    def _enviar(self, texto):
        print(f"    VOZ →TG {texto}")


class ExecutorDuble:
    """Modela o LocalExecutor SEM browser: serializa por conta (anti-ban), idempotente por
    curso, e expõe `drenar_obitos` (a costura que alimenta o vigia). `matar` simula a morte
    de um filho com exit_code/stderr reais."""
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


def _curso(url, conta, plataforma, total):
    return captura.CursoLocal(url=url, conta=conta, plataforma=plataforma,
                              total_esperado=total)


def _prog(mapa):
    return lambda c: mapa.get(c, (0, 0))


def main():
    tmp = tempfile.mkdtemp(prefix="athena-dryrun-")
    cpath = os.path.join(tmp, "controle.yaml")
    lock_dir = os.path.join(tmp, "locks")
    aut_dir = os.path.join(tmp, "autopsias")
    print(f"[tmpdir] {tmp}")

    voz = VozLog()
    alertas = Alertas(None, [])            # tg=None -> só-log (fail-safe, Athena muda)
    ex = ExecutorDuble({C_HOT: "rodrigo", C_MK: "ana"})
    cursos = [_curso(C_HOT, "rodrigo", "hotmart", 18),
              _curso(C_MK, "ana", "memberkit", 5)]
    estado, voo = {}, {}
    reais = dict(controle=controle, controle_path=cpath, disjuntor=disjuntor,
                 vigia=vigia, causa=causa, alertas=alertas, lock_dir=lock_dir,
                 autopsia_dir=aut_dir)

    # ---------------------------------------------------------------- (a)
    cena("(a) CONTROLE (P5): memberkit PAUSADO no controle.yaml NÃO dispara")
    controle.pausar_plataforma(cpath, "memberkit")
    print(f"    controle.yaml -> {open(cpath).read().strip()!r}")
    ex.disparos.clear()
    athena_local.ciclo_local(cursos, ex, _prog({C_HOT: (0, 18), C_MK: (0, 5)}), voz, voo,
                             estado, agora=1000.0, **reais)
    print(f"    >> disparados neste ciclo: {ex.disparos}")
    assert C_MK not in ex.disparos and C_HOT in ex.disparos
    print("    RESULTADO: hotmart disparou; memberkit (pausado) NÃO. ✔")

    # ---------------------------------------------------------------- (b1)
    cena("(b1) MORTE TRANSITÓRIA (SIGKILL): autópsia -> causa 'relancar' -> re-tenta")
    ex.matar(C_HOT, exit_code=-9)          # SIGKILL: transitório de recurso/SO
    print("    (o filho hotmart morreu por SIGKILL, exit -9)")
    ex.disparos.clear()
    athena_local.ciclo_local(cursos, ex, _prog({C_HOT: (0, 18), C_MK: (0, 5)}), voz, voo,
                             estado, agora=1100.0, **reais)
    print(f"    >> causa-raiz classificada: {estado[C_HOT].get('ultima_causa')!r}")
    print(f"    >> disj_falhas={estado[C_HOT].get('disj_falhas')} | re-disparou: "
          f"{C_HOT in ex.disparos}")
    assert C_HOT in ex.disparos
    print("    RESULTADO: transitório -> disjuntor contou a falha e RE-TENTOU. ✔")

    # ---------------------------------------------------------------- (b2)
    cena("(b2) MORTE por SESSÃO (irredutível): autópsia -> 'escalar_reseed' -> "
         "NÃO martela + ALERTA")
    ex.matar(C_HOT, exit_code=1,
             stderr="ERROR playwright SessionExpiredError: please log in again")
    print("    (o filho hotmart morreu com stderr de SESSÃO EXPIRADA)")
    ex.disparos.clear()
    athena_local.ciclo_local(cursos, ex, _prog({C_HOT: (0, 18), C_MK: (0, 5)}), voz, voo,
                             estado, agora=1200.0, **reais)
    print(f"    >> causa-raiz: {estado[C_HOT].get('ultima_causa')!r} | "
          f"irredutivel={estado[C_HOT].get('irredutivel')}")
    print(f"    >> re-disparou hotmart? {C_HOT in ex.disparos}  (esperado: False)")
    assert estado[C_HOT].get("irredutivel") is True and C_HOT not in ex.disparos
    print("    RESULTADO: sessão morta = IRREDUTÍVEL -> escala ao humano, NÃO martela. ✔")

    # ---------------------------------------------------------------- (c)
    cena("(c) BATIMENTO (P2): mesmo MUDO, LOGA a linha 'viva:' que iria pro Telegram")

    async def _noop(_):
        return None

    ex2 = ExecutorDuble({C_HOT: "rodrigo"})
    ex2.disparar(C_HOT)                    # 1 captura ativa p/ o resumo refletir
    asyncio.run(athena_local.rodar(
        [_curso(C_HOT, "rodrigo", "hotmart", 18)], ex2, _prog({C_HOT: (3, 18)}), voz,
        sleep=_noop, max_iters=1, intervalo_s=0.0, batimento=batimento,
        batimento_intervalo=0.0, disjuntor=disjuntor, vigia=vigia, causa=causa,
        alertas=alertas, controle=controle, controle_path=cpath, lock_dir=lock_dir,
        autopsia_dir=aut_dir))
    print("    RESULTADO: acima, LOG[athena.batimento] 'viva: ...' provou o pulso "
          "(sem POST, pois MUTED). ✔")

    print("\n" + "#" * 78)
    print("# DRY-RUN OK: (a) pausa, (b) autópsia->causa->disjuntor, (c) batimento — "
          "todos observados.")
    print("#" * 78)


if __name__ == "__main__":
    main()
