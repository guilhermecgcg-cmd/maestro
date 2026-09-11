"""PORTÃO DE CARGA DA MÁQUINA — não disparar motor num Mac sufocado.

INCIDENTE 10/09 23:33–23:55: uma transcrição pesada de OUTRO programa (100% CPU, swap
6,8/7,2 GB, carga média de 1 min até 24 num M1 de 8 núcleos e 8 GB) somada a 5-7 Chromes
headless fez o `Page.goto` estourar 30 s em quase todas as frentes: 5 disjuntores abertos
(Kiwify, Hubla, Memberkit, Stoa, Kajabi) e alertas "causa desconhecida". Cada disparo
naquela máquina só fabricava mais uma morte (e mais uma falha no disjuntor).

O QUE ESTE MÓDULO DECIDE (e só isto): dada uma leitura barata da máquina, o disparo de um
motor pode acontecer AGORA? `PortaoCarga.avaliar(motores_ativos)` devolve None (libera) ou
o MOTIVO do adiamento. Quem ADIA é o `LocalExecutor.disparar` (levanta
`MaquinaSobrecarregada` antes de qualquer spawn/lock); quem registra a decisão e o aviso
agregado é o loop (`athena_local`). Adiamento NÃO é falha.

CRITÉRIOS (qualquer um sobrecarrega):
  - carga média de 1 min > ATHENA_CARGA_MAX (default 1,5 × núcleos; <= 0 desliga);
  - memória livre < ATHENA_MEM_LIVRE_MIN_PCT (default 10; <= 0 desliga);
  - teto opcional de motores simultâneos ATHENA_MAX_MOTORES (default 0 = sem teto).

SENSOR (barato e robusto no macOS, sem subprocesso):
  - carga: `os.getloadavg()` (syscall);
  - memória livre: `sysctl kern.memorystatus_level` via `sysctlbyname` (ctypes) — é o
    "System-wide memory free percentage" que o `memory_pressure` imprime (conferido nesta
    máquina: os dois deram 37%). NÃO se usa "Pages free" do vm_stat: o macOS mantém as
    páginas livres perto de zero de propósito (usa inactive/compressor) — daria "sem
    memória" o tempo todo. Fallback Linux: MemAvailable/MemTotal de /proc/meminfo.
  - FALHA DE LEITURA => o critério daquele valor NÃO adia (fail-open) e o log avisa UMA vez
    por transição (WARNING em athena.carga -> loop.err). Por quê fail-open: um sensor
    quebrado que ADIASSE tudo pararia a captura inteira em silêncio ("0 aulas"); aberto,
    o comportamento volta a ser o de antes deste portão — nunca pior.

HISTERESE + ESCALONAMENTO (achado da revisão r6): a carga média de 1 min e o
`kern.memorystatus_level` demoram a refletir um motor recém-lançado (Chrome sobe, aloca e
só DEPOIS pesa na média). Sem histerese, a carga caindo LOGO abaixo do limite liberava
TODAS as contas livres no MESMO ciclo — 5-7 Chromes numa máquina ainda carregada, o
padrão do incidente. Agora:
  - HISTERESE por critério: entrou em sobrecarga (carga > máx / memória < mín), só SAI
    abaixo de um limite MENOR — carga <= ATHENA_CARGA_LIBERA (default 0,8 × máx) e
    memória livre >= ATHENA_MEM_LIVRE_LIBERA_PCT (default mín + 5 p.p.). Entre os dois
    limites o estado anterior vale (adiando continua adiando; liberado continua liberado).
  - ESCALONAMENTO (rampa) na SAÍDA da sobrecarga: no máximo ATHENA_CARGA_DISPAROS_POR_CICLO
    (default 2; 0 = sem escalonamento) disparos NOVOS por ciclo do loop, até um ciclo
    inteiro passar sem ninguém adiado (o represamento foi servido) — aí volta ao normal,
    sem teto. Fora da saída da sobrecarga NADA muda (máquina sã nunca é escalonada).
    O ciclo é sinalizado pelo loop (`novo_ciclo()`); se ninguém sinalizar por
    `JANELA_CICLO_MAX_S`, a janela vira sozinha (o escalonamento nunca vira trava eterna).
  - Adiar pela rampa também NÃO é falha (`Adiamento.escalonamento=True`: o loop não o
    conta como sobrecarga no episódio do aviso — a máquina já aliviou).
  - `retomar_sobrecarga()`: o loop reinicia DURANTE um episódio persistido => o portão
    nasce em sobrecarga (a histerese atravessa o reinício; senão a 1ª leitura entre os
    dois limites liberava todo mundo de uma vez logo depois do boot).
"""
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("athena.carga")

FATOR_NUCLEOS_PADRAO = 1.5          # carga máxima default = 1,5 × núcleos
MEM_LIVRE_MIN_PCT_PADRAO = 10.0     # memória livre mínima default (%)
FATOR_LIBERA_CARGA_PADRAO = 0.8     # histerese: em sobrecarga, só libera com carga <= 0,8 × máx
MARGEM_LIBERA_MEM_PP_PADRAO = 5.0   # histerese: ... e memória livre >= mín + 5 p.p.
DISPAROS_POR_CICLO_PADRAO = 2       # escalonamento na saída da sobrecarga (0 = desliga)
JANELA_CICLO_MAX_S = 600.0          # sem novo_ciclo() por 10 min, a janela da rampa vira só


@dataclass(frozen=True)
class LeituraCarga:
    """Uma leitura da máquina. None = aquele valor não pôde ser lido."""
    carga_1min: Optional[float]
    nucleos: Optional[int]
    mem_livre_pct: Optional[float]


@dataclass(frozen=True)
class Adiamento:
    """Veredito de ADIAR um disparo. `escalonamento=True` = a máquina já saiu da
    sobrecarga, mas a rampa segura o disparo para o ciclo seguinte (o sensor ainda não
    reflete os motores recém-lançados) — o loop não conta isso como sobrecarga."""
    motivo: str
    escalonamento: bool = False


# --------------------------------------------------------------------------
# sensor real
# --------------------------------------------------------------------------
_LIBC = []                           # handle da libc (carregado 1x, só se precisar)


def _sysctl_uint32(nome: bytes) -> Optional[int]:
    """Lê um sysctl inteiro de 32 bits via `sysctlbyname` (sem fork/exec). None se o
    sysctl não existe (não-macOS) ou a chamada falha."""
    try:
        import ctypes
        import ctypes.util
        if not _LIBC:
            libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib",
                               use_errno=True)
            fn = libc.sysctlbyname
            fn.argtypes = [ctypes.c_char_p, ctypes.c_void_p,
                           ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p,
                           ctypes.c_size_t]
            fn.restype = ctypes.c_int
            _LIBC.append(fn)
        valor = ctypes.c_uint32(0)
        tam = ctypes.c_size_t(ctypes.sizeof(valor))
        if _LIBC[0](nome, ctypes.byref(valor), ctypes.byref(tam), None, 0) != 0:
            return None
        return int(valor.value)
    except Exception:
        return None


def _mem_livre_pct_macos() -> Optional[float]:
    v = _sysctl_uint32(b"kern.memorystatus_level")
    if v is None or not 0 <= v <= 100:
        return None
    return float(v)


def _mem_livre_pct_linux() -> Optional[float]:
    try:
        campos = {}
        with open("/proc/meminfo") as f:
            for linha in f:
                nome, _, resto = linha.partition(":")
                campos[nome.strip()] = float(resto.split()[0])
        return 100.0 * campos["MemAvailable"] / campos["MemTotal"]
    except (OSError, KeyError, ValueError, IndexError, ZeroDivisionError):
        return None


def ler_carga() -> LeituraCarga:
    """Leitura REAL da máquina (barata: 1 syscall + 1 sysctl). Nunca levanta; o que não
    der para ler vem None (o portão trata como desconhecido => não adia por ele)."""
    try:
        carga = float(os.getloadavg()[0])
    except (OSError, AttributeError):
        carga = None
    mem = _mem_livre_pct_macos()
    if mem is None:
        mem = _mem_livre_pct_linux()
    return LeituraCarga(carga_1min=carga, nucleos=os.cpu_count(), mem_livre_pct=mem)


# --------------------------------------------------------------------------
# o portão
# --------------------------------------------------------------------------
def _fmt_num(v, casas=1) -> str:
    return "?" if v is None else f"{v:.{casas}f}"


class PortaoCarga:
    """Veredito de disparo pela carga da máquina. `sensor()` -> LeituraCarga é injetável
    (os testes nunca dependem da carga real desta máquina); `relogio()` (monotônico) só
    serve ao fallback da janela da rampa.

    `carga_max`: None => FATOR_NUCLEOS_PADRAO × núcleos da leitura; <= 0 => critério
    desligado. `mem_livre_min_pct` <= 0 => critério desligado. `max_motores` 0 => sem
    teto. `carga_libera` (None/<=0 => FATOR_LIBERA_CARGA_PADRAO × máx) e
    `mem_livre_libera_pct` (None/<=0 => mín + MARGEM_LIBERA_MEM_PP_PADRAO): limites de
    SAÍDA da sobrecarga (histerese; nunca mais frouxos que os de entrada).
    `disparos_por_ciclo`: teto de disparos novos por ciclo na SAÍDA da sobrecarga (0 =
    sem escalonamento)."""

    def __init__(self, *, sensor=ler_carga, carga_max=None,
                 mem_livre_min_pct=MEM_LIVRE_MIN_PCT_PADRAO, max_motores=0,
                 carga_libera=None, mem_livre_libera_pct=None,
                 disparos_por_ciclo=DISPAROS_POR_CICLO_PADRAO, relogio=time.monotonic):
        self._sensor = sensor
        self._relogio = relogio
        self.carga_max = carga_max
        self.mem_livre_min_pct = float(mem_livre_min_pct)
        self.max_motores = max(int(max_motores), 0)
        self.carga_libera = carga_libera
        self.mem_livre_libera_pct = mem_livre_libera_pct
        self.disparos_por_ciclo = max(int(disparos_por_ciclo), 0)
        self._sensor_avisado = False          # latch do aviso de sensor ilegível
        # HISTERESE: travas POR CRITÉRIO (entrou acima do limite de entrada; só solta
        # abaixo do de saída). Qualquer trava presa = em sobrecarga.
        self._preso_carga = False
        self._preso_mem = False
        # ESCALONAMENTO: rampa ativa desde a saída da sobrecarga; contagem da JANELA (um
        # ciclo do loop) e se ALGUÉM foi adiado nela (rampa só termina numa janela limpa).
        self._rampa = False
        self._disparos_janela = 0
        self._adiou_janela = False
        self._inicio_janela = self._agora()

    @classmethod
    def do_ambiente(cls, *, sensor=ler_carga, env=None):
        """Limites do ambiente: ATHENA_CARGA_MAX, ATHENA_MEM_LIVRE_MIN_PCT,
        ATHENA_MAX_MOTORES, ATHENA_CARGA_LIBERA, ATHENA_MEM_LIVRE_LIBERA_PCT,
        ATHENA_CARGA_DISPAROS_POR_CICLO. Valor malformado => default + WARNING (nunca
        derruba o boot)."""
        env = os.environ if env is None else env

        def _num(nome, conv, default):
            bruto = (env.get(nome) or "").strip()
            if not bruto:
                return default
            try:
                return conv(bruto.replace(",", "."))
            except ValueError:
                log.warning("portão de carga: %s=%r malformado — uso o default %r",
                            nome, bruto, default)
                return default

        return cls(sensor=sensor,
                   carga_max=_num("ATHENA_CARGA_MAX", float, None),
                   mem_livre_min_pct=_num("ATHENA_MEM_LIVRE_MIN_PCT", float,
                                          MEM_LIVRE_MIN_PCT_PADRAO),
                   max_motores=max(_num("ATHENA_MAX_MOTORES", int, 0), 0),
                   carga_libera=_num("ATHENA_CARGA_LIBERA", float, None),
                   mem_livre_libera_pct=_num("ATHENA_MEM_LIVRE_LIBERA_PCT", float, None),
                   disparos_por_ciclo=max(_num("ATHENA_CARGA_DISPAROS_POR_CICLO", int,
                                               DISPAROS_POR_CICLO_PADRAO), 0))

    # ---- limites -----------------------------------------------------------
    def _agora(self) -> float:
        try:
            return float(self._relogio())
        except Exception:
            return 0.0

    def _carga_ligada(self) -> bool:
        return self.carga_max is None or self.carga_max > 0

    def _mem_ligada(self) -> bool:
        return self.mem_livre_min_pct > 0

    def _carga_max_efetiva(self, nucleos) -> Optional[float]:
        if self.carga_max is not None:
            return self.carga_max if self.carga_max > 0 else None
        if not nucleos:
            return None
        return FATOR_NUCLEOS_PADRAO * float(nucleos)

    def _carga_libera_efetiva(self, nucleos) -> Optional[float]:
        """Limite de SAÍDA da carga: nunca acima do de entrada (histerese >= 0)."""
        maximo = self._carga_max_efetiva(nucleos)
        if maximo is None:
            return None
        if self.carga_libera is not None and self.carga_libera > 0:
            return min(float(self.carga_libera), maximo)
        return FATOR_LIBERA_CARGA_PADRAO * maximo

    def _mem_libera_efetiva(self) -> Optional[float]:
        """Limite de SAÍDA da memória: nunca abaixo do de entrada, nunca acima de 100%."""
        if not self._mem_ligada():
            return None
        if self.mem_livre_libera_pct is not None and self.mem_livre_libera_pct > 0:
            libera = float(self.mem_livre_libera_pct)
        else:
            libera = self.mem_livre_min_pct + MARGEM_LIBERA_MEM_PP_PADRAO
        return min(max(libera, self.mem_livre_min_pct), 100.0)

    def descrever(self) -> str:
        cm = ("desligado" if self.carga_max is not None and self.carga_max <= 0 else
              f"{self.carga_max:.1f}" if self.carga_max is not None else
              f"{FATOR_NUCLEOS_PADRAO}×núcleos")
        if not self._carga_ligada():
            cl = "—"
        elif self.carga_libera is not None and self.carga_libera > 0:
            cl = f"{self.carga_libera:.1f}"
        else:
            cl = f"{FATOR_LIBERA_CARGA_PADRAO}×máx"
        mm = (f"{self.mem_livre_min_pct:.0f}%" if self._mem_ligada() else "desligado")
        ml = (f"{self._mem_libera_efetiva():.0f}%" if self._mem_ligada() else "—")
        teto = str(self.max_motores) if self.max_motores else "sem teto"
        rampa = (f"no máx {self.disparos_por_ciclo} disparo(s) novo(s) por ciclo"
                 if self.disparos_por_ciclo else "sem escalonamento")
        return (f"carga máx {cm}; memória livre mín {mm}; motores simultâneos {teto}; "
                f"histerese: em sobrecarga só libero com carga <= {cl} e memória livre "
                f">= {ml}; saída da sobrecarga: {rampa}")

    def _aviso_sensor(self, ilegivel):
        """WARNING só na TRANSIÇÃO (ok -> ilegível); a volta ao normal re-arma."""
        if ilegivel and not self._sensor_avisado:
            self._sensor_avisado = True
            log.warning("portão de carga: sensor ilegível (%s) — portão ABERTO para esse "
                        "critério (fail-open: sem leitura não adio disparo)", ilegivel)
        elif not ilegivel and self._sensor_avisado:
            self._sensor_avisado = False
            log.warning("portão de carga: sensor voltou a ler normalmente")

    # ---- histerese ---------------------------------------------------------
    def em_sobrecarga(self) -> bool:
        return self._preso_carga or self._preso_mem

    def _prender(self, carga, mem):
        """Aplica as travas novas; a TRANSIÇÃO sobrecarga -> livre inicia a rampa."""
        estava = self.em_sobrecarga()
        self._preso_carga, self._preso_mem = bool(carga), bool(mem)
        if estava and not self.em_sobrecarga():
            self._iniciar_rampa()

    def retomar_sobrecarga(self):
        """O loop reiniciou DURANTE um episódio de sobrecarga (persistido): nasce preso nos
        critérios ligados — a próxima leitura só libera abaixo dos limites de SAÍDA (e aí
        entra a rampa). Sem isto, uma leitura entre os dois limites logo depois do boot
        liberava todas as contas livres de uma vez. Um critério ilegível solta na 1ª
        leitura (fail-open de sempre)."""
        self._preso_carga = self._carga_ligada()
        self._preso_mem = self._mem_ligada()
        if self.em_sobrecarga():
            log.warning("portão de carga: retomei um episódio de sobrecarga do reinício — "
                        "só libero abaixo dos limites de saída (histerese)")

    def avaliar_leitura(self, leitura: LeituraCarga) -> Optional[str]:
        """None = libera; str = motivo do adiamento. Com HISTERESE: o estado anterior
        (preso/livre) decide entre o limite de entrada e o de saída de cada critério.
        Leitura ilegível de um critério solta a trava dele (fail-open)."""
        carga_max = self._carga_max_efetiva(leitura.nucleos)
        carga_lib = self._carga_libera_efetiva(leitura.nucleos)
        mem_min = self.mem_livre_min_pct if self._mem_ligada() else None
        mem_lib = self._mem_libera_efetiva()
        ilegiveis = []
        if self._carga_ligada():                                  # critério de carga ligado
            if leitura.carga_1min is None:
                ilegiveis.append("carga")
            if self.carga_max is None and not leitura.nucleos:
                ilegiveis.append("núcleos")
        if mem_min is not None and leitura.mem_livre_pct is None:
            ilegiveis.append("memória livre")
        self._aviso_sensor(", ".join(ilegiveis))
        carga = leitura.carga_1min
        mem = leitura.mem_livre_pct
        if carga_max is None or carga is None:
            preso_carga = False                                   # desligado/ilegível: solta
        elif self._preso_carga:
            preso_carga = carga > carga_lib                       # só sai ABAIXO do de saída
        else:
            preso_carga = carga > carga_max
        if mem_min is None or mem is None:
            preso_mem = False
        elif self._preso_mem:
            preso_mem = mem < mem_lib
        else:
            preso_mem = mem < mem_min
        self._prender(preso_carga, preso_mem)
        if not self.em_sobrecarga():
            return None
        limites = []
        if carga_max is not None:
            limites.append(f"carga > {carga_max:.1f}")
        if mem_min is not None:
            limites.append(f"memória livre < {mem_min:.0f}%")
        saida = []
        if carga_lib is not None:
            saida.append(f"carga <= {carga_lib:.1f}")
        if mem_lib is not None:
            saida.append(f"memória livre >= {mem_lib:.0f}%")
        return (f"máquina sobrecarregada (carga {_fmt_num(carga)}, memória livre "
                f"{_fmt_num(mem, 0)}%) — adio se " + " ou ".join(limites)
                + ("; só libero com " + " e ".join(saida) if saida else ""))

    # ---- escalonamento (rampa) ---------------------------------------------
    def _iniciar_rampa(self):
        if self.disparos_por_ciclo <= 0:
            return
        self._rampa = True
        self._disparos_janela = 0
        log.warning("portão de carga: saí da sobrecarga — escalono os disparos (no máx %d "
                    "por ciclo) até o represamento ser servido", self.disparos_por_ciclo)

    def em_rampa(self) -> bool:
        return self._rampa

    def novo_ciclo(self):
        """Fronteira de ciclo (o loop chama 1x por ciclo, antes das passadas). A rampa
        termina quando uma janela INTEIRA passou sem ninguém adiado (o represamento foi
        servido); a contagem de disparos da janela zera."""
        if self._rampa and not self._adiou_janela:
            self._rampa = False
            log.warning("portão de carga: escalonamento concluído — disparos sem teto "
                        "por ciclo de novo")
        self._disparos_janela = 0
        self._adiou_janela = False
        self._inicio_janela = self._agora()

    def _virar_janela_se_expirou(self):
        """Fallback: ninguém sinalizou o ciclo por JANELA_CICLO_MAX_S => a janela vira
        sozinha (a rampa nunca vira trava eterna para um chamador que não conhece o
        `novo_ciclo`)."""
        if self._agora() - self._inicio_janela >= JANELA_CICLO_MAX_S:
            self.novo_ciclo()

    def registrar_disparo(self):
        """Um motor SUBIU agora (o executor chama depois do spawn): conta na janela."""
        if self._rampa:
            self._disparos_janela += 1

    def _veto_da_rampa(self) -> Optional[Adiamento]:
        if not self._rampa or self.disparos_por_ciclo <= 0:
            return None
        if self._disparos_janela < self.disparos_por_ciclo:
            return None
        return Adiamento(
            f"escalonamento na saída da sobrecarga: {self._disparos_janela} motor(es) "
            f"já disparado(s) neste ciclo (máx {self.disparos_por_ciclo} por ciclo — "
            f"ATHENA_CARGA_DISPAROS_POR_CICLO); a carga ainda não reflete os recém-"
            f"lançados, este fica para o próximo ciclo", escalonamento=True)

    # ---- veredito ------------------------------------------------------------
    def decidir(self, motores_ativos: int = 0) -> Optional[Adiamento]:
        """Veredito do disparo AGORA: None (libera) ou `Adiamento`. Ordem: teto de
        motores (não depende de sensor) -> sobrecarga com histerese -> rampa da saída.
        Sensor que LEVANTA => fail-open para a carga/memória (solta as travas) + aviso no
        log; a rampa (contagem, não sensor) segue valendo."""
        self._virar_janela_se_expirou()
        veto = None
        if self.max_motores and int(motores_ativos) >= self.max_motores:
            veto = Adiamento(f"teto de {self.max_motores} motor(es) simultâneo(s) atingido "
                             f"({int(motores_ativos)} ativo(s)) — ATHENA_MAX_MOTORES")
        else:
            try:
                leitura = self._sensor()
            except Exception as e:
                self._aviso_sensor(f"{type(e).__name__}: {str(e)[:80]}")
                self._prender(False, False)
                leitura = None
            if leitura is not None:
                motivo = self.avaliar_leitura(leitura)
                if motivo:
                    veto = Adiamento(motivo)
            if veto is None:
                veto = self._veto_da_rampa()
        if veto is not None:
            self._adiou_janela = True
        return veto

    def avaliar(self, motores_ativos: int = 0) -> Optional[str]:
        """Compat: o motivo do `decidir` (None = libera)."""
        veto = self.decidir(motores_ativos)
        return veto.motivo if veto is not None else None
