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
"""
import logging
import os
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("athena.carga")

FATOR_NUCLEOS_PADRAO = 1.5          # carga máxima default = 1,5 × núcleos
MEM_LIVRE_MIN_PCT_PADRAO = 10.0     # memória livre mínima default (%)


@dataclass(frozen=True)
class LeituraCarga:
    """Uma leitura da máquina. None = aquele valor não pôde ser lido."""
    carga_1min: Optional[float]
    nucleos: Optional[int]
    mem_livre_pct: Optional[float]


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
    (os testes nunca dependem da carga real desta máquina).

    `carga_max`: None => FATOR_NUCLEOS_PADRAO × núcleos da leitura; <= 0 => critério
    desligado. `mem_livre_min_pct` <= 0 => critério desligado. `max_motores` 0 => sem
    teto."""

    def __init__(self, *, sensor=ler_carga, carga_max=None,
                 mem_livre_min_pct=MEM_LIVRE_MIN_PCT_PADRAO, max_motores=0):
        self._sensor = sensor
        self.carga_max = carga_max
        self.mem_livre_min_pct = float(mem_livre_min_pct)
        self.max_motores = max(int(max_motores), 0)
        self._sensor_avisado = False          # latch do aviso de sensor ilegível

    @classmethod
    def do_ambiente(cls, *, sensor=ler_carga, env=None):
        """Limites do ambiente: ATHENA_CARGA_MAX, ATHENA_MEM_LIVRE_MIN_PCT,
        ATHENA_MAX_MOTORES. Valor malformado => default + WARNING (nunca derruba o boot)."""
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
                   max_motores=max(_num("ATHENA_MAX_MOTORES", int, 0), 0))

    def _carga_max_efetiva(self, nucleos) -> Optional[float]:
        if self.carga_max is not None:
            return self.carga_max if self.carga_max > 0 else None
        if not nucleos:
            return None
        return FATOR_NUCLEOS_PADRAO * float(nucleos)

    def descrever(self) -> str:
        cm = ("desligado" if self.carga_max is not None and self.carga_max <= 0 else
              f"{self.carga_max:.1f}" if self.carga_max is not None else
              f"{FATOR_NUCLEOS_PADRAO}×núcleos")
        mm = (f"{self.mem_livre_min_pct:.0f}%" if self.mem_livre_min_pct > 0
              else "desligado")
        teto = str(self.max_motores) if self.max_motores else "sem teto"
        return f"carga máx {cm}; memória livre mín {mm}; motores simultâneos {teto}"

    def _aviso_sensor(self, ilegivel):
        """WARNING só na TRANSIÇÃO (ok -> ilegível); a volta ao normal re-arma."""
        if ilegivel and not self._sensor_avisado:
            self._sensor_avisado = True
            log.warning("portão de carga: sensor ilegível (%s) — portão ABERTO para esse "
                        "critério (fail-open: sem leitura não adio disparo)", ilegivel)
        elif not ilegivel and self._sensor_avisado:
            self._sensor_avisado = False
            log.warning("portão de carga: sensor voltou a ler normalmente")

    def avaliar_leitura(self, leitura: LeituraCarga) -> Optional[str]:
        """None = libera; str = motivo do adiamento. Puro sobre a leitura (fora o latch do
        aviso de sensor)."""
        carga_max = self._carga_max_efetiva(leitura.nucleos)
        mem_min = self.mem_livre_min_pct if self.mem_livre_min_pct > 0 else None
        ilegiveis = []
        if self.carga_max is None or self.carga_max > 0:          # critério de carga ligado
            if leitura.carga_1min is None:
                ilegiveis.append("carga")
            if self.carga_max is None and not leitura.nucleos:
                ilegiveis.append("núcleos")
        if mem_min is not None and leitura.mem_livre_pct is None:
            ilegiveis.append("memória livre")
        self._aviso_sensor(", ".join(ilegiveis))
        sobre_carga = (carga_max is not None and leitura.carga_1min is not None
                       and leitura.carga_1min > carga_max)
        sobre_mem = (mem_min is not None and leitura.mem_livre_pct is not None
                     and leitura.mem_livre_pct < mem_min)
        if not (sobre_carga or sobre_mem):
            return None
        limites = []
        if carga_max is not None:
            limites.append(f"carga > {carga_max:.1f}")
        if mem_min is not None:
            limites.append(f"memória livre < {mem_min:.0f}%")
        return (f"máquina sobrecarregada (carga {_fmt_num(leitura.carga_1min)}, "
                f"memória livre {_fmt_num(leitura.mem_livre_pct, 0)}%) — adio se "
                + " ou ".join(limites))

    def avaliar(self, motores_ativos: int = 0) -> Optional[str]:
        """Veredito do disparo AGORA. O teto de motores vem primeiro (não depende de
        sensor); sensor que LEVANTA => fail-open (None) + aviso no log."""
        if self.max_motores and int(motores_ativos) >= self.max_motores:
            return (f"teto de {self.max_motores} motor(es) simultâneo(s) atingido "
                    f"({int(motores_ativos)} ativo(s)) — ATHENA_MAX_MOTORES")
        try:
            leitura = self._sensor()
        except Exception as e:
            self._aviso_sensor(f"{type(e).__name__}: {str(e)[:80]}")
            return None
        return self.avaliar_leitura(leitura)
