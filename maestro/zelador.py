"""ZELADOR DE SESSÃO — o AGENDADOR do daemon (P7 etapa 2).

O problema: cada sessão de plataforma morre sozinha (em horas numas, dias noutras) e quem
descobre é a captura, que falha e escala. O zelador faz duas coisas, e só elas:

1. MANTER VIVA a sessão de conta OCIOSA: de tempos em tempos (intervalo SORTEADO por conta,
   6–12 h), numa conta sem captura há >= X h, o daemon spawna `python -m motor.zelador
   <plat> <url> --conta <conta>` — a sonda DA PLATAFORMA (o mesmo `ensure_session` que
   abre toda captura: injeta, sonda, re-persiste quando é seguro), com o mesmo navegador e
   o mesmo perfil da captura, SEGURANDO o mesmo lock durável da conta.
2. Quando SÓ O HUMANO resolve, avisar UMA vez, agregado, com o comando exato
   (`uv run scripts/reseed.py plat:conta ...`; com a Stoa, prefixado pela árvore dela em
   `ATHENA_MOTOR_DIR_STOA=`) — e com antecedência quando o relógio da sessão é DURO (não
   avança com o uso) e vence em menos de N dias.

INVIOLÁVEIS (o que, relaxado, quebra o projeto):
  - DESLIGADO por padrão (ATHENA_ZELADOR_ATIVO): "seco" só grava o status; "1" zela.
  - Nunca junto de captura: o zelo segura o lock da conta E das PARCEIRAS (contas que
    partilham o arquivo de sessão ou o perfil); a captura vê ContaOcupada e espera.
  - 1 zelo por vez (global), teto por hora, adiado sob carga (portão mais estrito que o da
    captura — o zelo é opcional).
  - O resultado do zelo NÃO é óbito de captura: nada de autópsia, disjuntor, flap, bench.
  - Morte PROVADA (exit 3 + linha `morta`) => `aguardando-humano` e o zelador PARA de tocar
    na conta (zero navegação deslogada repetida) até alguém mexer na sessão (M4). Morte de
    plataforma com PROVA FRACA (Stoa, Alpaclass, Hubla) exige uma 2ª morte, >= 30 min
    depois. Na Stoa e na Alpaclass cada morte só conta com `prova: positiva` (a sonda
    TRI-ESTADO do zelo leu a tela/código de login; rede/5xx = inconclusivo): a confirmação
    são DUAS provas positivas de login, nunca duas falhas de rede. Inconclusivo NUNCA vira
    morte (backoff).
  - A sonda usa a URL de um curso da unidade que a captura PODE rodar — nunca um travado,
    benchado ou sem acesso (um 403 de um produto viraria "login necessário" da conta).
  - Status (`sessoes-status.json`) sem credencial: só rótulos, epochs e tipos de exceção;
    escrita atômica. É o insumo do /sitrep e do `reseed.py tudo-morto`.
  - Nunca loga, nunca digita, nunca aceita termo: quem prova é a sonda da plataforma com
    `allow_reseed=False` (motor/zelador.py).

M4 (rearme): o latch de reseed de um curso só sai com AÇÃO HUMANA PROVADA — o CARIMBO que
`scripts/reseed.py` (motor) grava depois de um login concluído, com o lock nas mãos
(`<lock_dir>/<sha256(conta)[:16]>.reseed.json`, `LocalExecutor.carimbo_reseed`) — MAIS um
zelo que prove a sessão viva. Rearma só os cursos travados ANTES do carimbo; um carimbo novo
põe a unidade na frente da fila (zela já). O mtime do arquivo de sessão/perfil NÃO serve
para rearmar: o próprio zelo re-persiste o arquivo num viva e o Chrome mexe na raiz do
perfil (achado bloqueante da revisão: dois vivas seguidos rearmavam sem ninguém ter logado,
reabrindo o laço captura-morre -> zelo-viva -> rearma). O mtime segue só como gatilho para
RE-SONDAR uma conta aguardando-humano (1 zelo por mudança; nunca rearma).
"""
import json
import logging
import os
import random
import re
import shlex
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime

from maestro.adaptadores import captura

log = logging.getLogger("athena.zelador")

MODO_ENV = "ATHENA_ZELADOR_ATIVO"
STATUS_ENV = "ATHENA_SESSOES_STATUS"
STATUS_PADRAO = os.path.join(os.path.expanduser("~"), ".athena-local", "sessoes-status.json")

DESLIGADO, SECO, LIGADO = "desligado", "seco", "ligado"

# ciclo de vida da sessão de uma unidade (o `status` do JSON)
VIVA = "viva"
AGUARDANDO = "aguardando-humano"
SUSPEITA = "morte-suspeita"
DESCONHECIDA = "desconhecida"
_STATUS = frozenset({VIVA, AGUARDANDO, SUSPEITA, DESCONHECIDA})

# resultados do motor (motor/zelador.py) que o agendador entende
R_VIVA, R_MORTA, R_INCONCLUSIVA = "viva", "morta", "inconclusiva"
R_SEM_PERFIL, R_SEM_SESSAO, R_OCUPADO = "sem-perfil", "sem-sessao-salva", "ocupado"
_RESULTADOS_5 = frozenset({R_SEM_PERFIL, R_SEM_SESSAO, R_OCUPADO})

# Plataformas cuja "morte" hoje é AUSÊNCIA de sinal positivo (doc do P7, seção 5): a sonda
# da Stoa lê navegação que falhou como morta; a da Alpaclass, rede/5xx; a da Hubla, "nenhum
# Bearer em 25 s". Até virarem tri-estado, a 1ª morte é só suspeita.
PROVA_FRACA = frozenset({"stoa", "alpaclass", "hubla"})

# Plataformas cujo motor tem a SONDA TRI-ESTADO do zelo (`sonda_zelador` do módulo de sessão:
# rede/5xx/429 = inconclusivo; só a tela/código de login = morta). Nelas uma morte só conta
# com `prova: positiva` na linha ZELADOR — um motor ou uma árvore da Stoa ANTIGOS (sem a
# sonda) que digam "morta" podem estar lendo uma queda de internet: INCONCLUSIVO.
SONDA_TRI_ESTADO = frozenset({"stoa", "alpaclass"})
PROVA_POSITIVA = "positiva"

# Arquivo de sessão DEFAULT do CLI de cada plataforma (relativo ao cwd do motor dela) —
# ESPELHO de motor/profiles.py::PLATAFORMAS[*].state_default. Só vale onde o daemon NÃO
# injeta o session_path (hotmart/stoa/kajabi, ou conta sem session_path no YAML). É a
# marca do M4: o reseed regrava ESTE arquivo.
_SESSAO_PADRAO = {
    "hotmart": ".hotmart-session.json", "memberkit": ".memberkit-session.json",
    "stoa": ".stoa-session.json", "kajabi": ".kajabi-session.json",
    "kiwify": ".kiwify-session.json", "nutror": ".nutror-session.json",
    "alpaclass": ".alpaclass-session.json", "hubla": ".hubla-session.json",
    "greenn": ".greenn-session.json", "curseduca": ".segueadi-session.json",
}

# Frases FIXAS do `detalhe` do motor/zelador.py (ESPELHO) + as do daemon. Fora delas só
# passa nome de exceção (CamelCase terminado em Error/Exception/...): o status nunca carrega
# texto cru — nem se o motor um dia imprimir algo errado na linha.
_DETALHES_FIXOS = frozenset({
    "plataforma desconhecida", "sem perfil nem conta", "perfil fixo de outra plataforma",
    "perfil da conta inexistente", "arquivo de sessao ausente", "perfil em uso",
    "modulo da plataforma ausente nesta arvore", "sonda sem prova",
    "sonda da plataforma provou", "interrompido", "Error",
    "watchdog", "sem linha do zelador", "detalhe descartado", "teto de tempo",
    "morte sem prova positiva",
})
_RE_EXCECAO = re.compile(r"[A-Z][A-Za-z0-9]{0,47}(Error|Exception|Timeout|Interrupt|Exit)")

# chaves do estado de máquina (`_t`) que vão para o disco e voltam no restart
_T_PERSISTIDO = frozenset({
    "intervalo_s", "proximo_ts", "ultimo_zelo_ts", "provado_ts", "expira_ts",
    "expira_prova_ts", "primeira_morte_ts", "morte_ts", "marca_na_morte",
    "ultima_captura_ts", "carimbo_visto",
})

_H = 3600.0
_DIA = 86400.0


# --- configuração ------------------------------------------------------------------------

@dataclass(frozen=True)
class ConfigZelador:
    """Limiares do agendador. Defaults = o desenho do P7 (X = 6 h; jitter 6–12 h; alerta
    preventivo a 3 dias); tudo calibrável pelo ambiente (`do_ambiente`)."""
    ociosa_s: float = 6 * _H              # conta sem captura há >= isto (a captura já renova)
    intervalo_min_s: float = 6 * _H       # intervalo entre zelos, sorteado por conta
    intervalo_max_s: float = 12 * _H
    intervalos_por_plataforma: dict = field(default_factory=dict)  # plat -> (min_s, max_s)
    preventivo_s: float = 3 * _DIA        # relógio DURO vencendo em menos disto => alerta
    max_por_hora: int = 3                 # teto de zelos por hora (global)
    adiar_s: float = 30 * 60              # adiado sob carga: tenta de novo depois disto
    confirmacao_s: float = 30 * 60        # prova fraca: a 2ª morte, >= isto depois da 1ª
    backoff_base_s: float = _H            # inconclusivo: 1 h, 2 h, 4 h... (teto: o intervalo)
    ocupado_s: float = 30 * 60            # o motor achou o perfil ocupado: tenta depois disto
    timeout_s: float = 600.0              # watchdog do zelo (o executor aplica)

    def intervalo(self, plataforma):
        return self.intervalos_por_plataforma.get(
            plataforma, (self.intervalo_min_s, self.intervalo_max_s))

    @classmethod
    def do_ambiente(cls, env=None):
        """ATHENA_ZELADOR_OCIOSA_H, ATHENA_ZELADOR_INTERVALO_H ("6-12"),
        ATHENA_ZELADOR_INTERVALO_H_<PLAT> ("3-4"), ATHENA_ZELADOR_PREVENTIVO_DIAS,
        ATHENA_ZELADOR_MAX_POR_HORA, ATHENA_ZELADOR_TIMEOUT_S. Malformado => default +
        WARNING (nunca derruba o boot)."""
        env = os.environ if env is None else env
        base = cls()

        def num(nome, default, conv=float):
            bruto = (env.get(nome) or "").strip()
            if not bruto:
                return default
            try:
                v = conv(bruto.replace(",", "."))
            except ValueError:
                log.warning("zelador: %s=%r malformado — uso o default %r", nome, bruto, default)
                return default
            if v <= 0:
                log.warning("zelador: %s=%r fora de faixa — uso o default %r", nome, bruto, default)
                return default
            return v

        def faixa(nome, default):
            bruto = (env.get(nome) or "").strip()
            if not bruto:
                return default
            try:
                a, b = (float(x.replace(",", ".")) * _H for x in bruto.split("-", 1))
            except ValueError:
                log.warning("zelador: %s=%r malformado (use 'min-max' em horas) — default",
                            nome, bruto)
                return default
            if not (0 < a <= b):
                log.warning("zelador: %s=%r fora de faixa — default", nome, bruto)
                return default
            return (a, b)

        imin, imax = faixa("ATHENA_ZELADOR_INTERVALO_H",
                           (base.intervalo_min_s, base.intervalo_max_s))
        prefixo = "ATHENA_ZELADOR_INTERVALO_H_"
        por_plat = {}
        for k in env:
            if k.startswith(prefixo) and k != prefixo:
                por_plat[k[len(prefixo):].lower()] = faixa(k, (imin, imax))
        return cls(ociosa_s=num("ATHENA_ZELADOR_OCIOSA_H", base.ociosa_s / _H) * _H,
                   intervalo_min_s=imin, intervalo_max_s=imax,
                   intervalos_por_plataforma=por_plat,
                   preventivo_s=num("ATHENA_ZELADOR_PREVENTIVO_DIAS",
                                    base.preventivo_s / _DIA) * _DIA,
                   max_por_hora=int(num("ATHENA_ZELADOR_MAX_POR_HORA", base.max_por_hora,
                                        conv=int)),
                   timeout_s=num("ATHENA_ZELADOR_TIMEOUT_S", base.timeout_s))


def modo_do_ambiente(env=None) -> str:
    """"1" => ligado; "seco" => só status; qualquer outra coisa (inclusive ausente, "0",
    "sim") => DESLIGADO. Fail-closed: só o valor exato liga um processo que abre navegador."""
    env = os.environ if env is None else env
    bruto = (env.get(MODO_ENV) or "").strip().lower()
    if bruto == "1":
        return LIGADO
    if bruto == "seco":
        return SECO
    return DESLIGADO


# --- o portão de carga do zelo (mais estrito que o da captura) ------------------------------

class PortaoZelo:
    """Veredito de carga para o ZELO, reusando o sensor e a regra do portão da captura
    (`maestro.carga`), com limiares mais estritos: carga <= FATOR × núcleos da leitura,
    memória livre >= MEM%, e no máximo N motores vivos. Sensor ilegível => fail-open
    naquele critério (a regra do `carga.PortaoCarga`)."""

    def __init__(self, sensor, *, fator, mem_livre_min_pct, max_capturas):
        from maestro import carga
        self._sensor = sensor
        self.fator = float(fator)
        self.max_capturas = int(max_capturas)
        self._portao = carga.PortaoCarga(sensor=sensor, carga_max=0,
                                         mem_livre_min_pct=mem_livre_min_pct)

    def descrever(self) -> str:
        return (f"carga máx {self.fator:g}×núcleos; memória livre mín "
                f"{self._portao.mem_livre_min_pct:.0f}%; máx {self.max_capturas} motor(es) vivo(s)")

    def avaliar(self, motores_ativos: int = 0):
        if int(motores_ativos) > self.max_capturas:
            return (f"{int(motores_ativos)} motor(es) vivo(s) — o zelo espera "
                    f"(máx {self.max_capturas})")
        try:
            leitura = self._sensor()
        except Exception:
            return None                                    # fail-open (o da captura também)
        nucleos = getattr(leitura, "nucleos", None)
        self._portao.carga_max = self.fator * nucleos if (self.fator > 0 and nucleos) else 0
        try:
            return self._portao.avaliar_leitura(leitura)
        except Exception:
            return None


def portao_do_ambiente(env=None, sensor=None):
    """O portão do zelo, ou None se esta árvore não tem o portão de carga (`maestro.carga`,
    daemon d093e1d+): aí fica só o GANCHO (o agendador segue sem adiar por carga).
    ATHENA_ZELADOR_CARGA_FATOR (0,75), ATHENA_ZELADOR_MEM_LIVRE_MIN_PCT (25),
    ATHENA_ZELADOR_MAX_CAPTURAS (1)."""
    try:
        from maestro import carga
    except ImportError:  # pragma: no cover — árvore sem o portão
        return None
    env = os.environ if env is None else env

    def num(nome, default):
        bruto = (env.get(nome) or "").strip()
        if not bruto:
            return default
        try:
            return float(bruto.replace(",", "."))
        except ValueError:
            log.warning("zelador: %s=%r malformado — uso o default %r", nome, bruto, default)
            return default

    return PortaoZelo(sensor or carga.ler_carga, fator=num("ATHENA_ZELADOR_CARGA_FATOR", 0.75),
                      mem_livre_min_pct=num("ATHENA_ZELADOR_MEM_LIVRE_MIN_PCT", 25.0),
                      max_capturas=int(num("ATHENA_ZELADOR_MAX_CAPTURAS", 1)))


# --- unidades de zelo -------------------------------------------------------------------------

@dataclass(frozen=True)
class Unidade:
    """Um LOGIN a manter vivo: (plataforma, conta, arquivo de sessão que o daemon injeta).
    Uma conta com DOIS arquivos (a Greenn: dois clubs) vira DUAS unidades — zeladas em série,
    cada uma com o seu arquivo, as duas sob o lock da mesma conta. `cursos` = os cursos da
    unidade (o rearme M4 mexe só neles; a sonda escolhe entre eles a cada zelo — ver
    `Zelador._url_da_sonda`); `url` = a do 1º (a identidade estável da unidade no status)."""
    chave: str
    plataforma: str
    conta: str
    url: str
    session_path: str
    cursos: tuple

    @property
    def alvo(self) -> str:
        """`plataforma:conta` — a sintaxe do `scripts/reseed.py` (que faz os dois clubs da
        Greenn em série num alvo só)."""
        return f"{self.plataforma}:{self.conta}"


def unidades_de(cursos) -> list:
    """Agrupa os cursos do cadastro em unidades de zelo. O `session_path` só distingue
    unidades onde o daemon o INJETA (spec com `session_env`); nas demais ele é inerte.
    Plataforma sem spec => fora (fail-closed, como o `_montar`)."""
    grupos = {}
    for c in cursos:
        spec = captura._PLATAFORMAS.get(c.plataforma)
        if spec is None:
            continue
        sess = (getattr(c, "session_path", "") or "") if spec.session_env else ""
        grupos.setdefault((c.plataforma, str(c.conta), sess), []).append(c)
    n_por_conta = {}
    for plat, conta, _ in grupos:
        n_por_conta[(plat, conta)] = n_por_conta.get((plat, conta), 0) + 1
    out = []
    for (plat, conta, sess), cs in grupos.items():
        chave = f"{plat}:{conta}"
        if n_por_conta[(plat, conta)] > 1:
            chave += "#" + (os.path.basename(sess) or "padrao")
        out.append(Unidade(chave=chave, plataforma=plat, conta=conta, url=cs[0].url,
                           session_path=sess, cursos=tuple(c.url for c in cs)))
    return out


def comando_reseed(alvos, *, motor_dir_stoa=None) -> str:
    """O comando EXATO que o alerta manda colar (no checkout do motor). Com alvo da Stoa,
    prefixado por `ATHENA_MOTOR_DIR_STOA=<árvore>` — a MESMA árvore em que o daemon roda a
    Stoa: o reseed é fail-closed sem ela (a Stoa mora noutra árvore) e recusaria."""
    lista = sorted(dict.fromkeys(alvos))
    cmd = "uv run scripts/reseed.py " + " ".join(lista)
    if motor_dir_stoa and any(str(a).split(":", 1)[0] == "stoa" for a in lista):
        cmd = f"ATHENA_MOTOR_DIR_STOA={shlex.quote(str(motor_dir_stoa))} " + cmd
    return cmd


# --- helpers -------------------------------------------------------------------------------------

def _iso(ts):
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts)).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError, TypeError):
        return None


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _detalhe_seguro(d) -> str:
    if isinstance(d, str) and (d in _DETALHES_FIXOS or _RE_EXCECAO.fullmatch(d)):
        return d
    return "detalhe descartado" if d else ""


def _sem_prova_positiva(r, plataforma) -> bool:
    """Morte (exit 3 + `morta`) numa plataforma de sonda tri-estado SEM `prova: positiva`."""
    linha = r.get("linha") or {}
    return (plataforma in SONDA_TRI_ESTADO and r.get("exit_code") == 3
            and linha.get("resultado") == R_MORTA and linha.get("prova") != PROVA_POSITIVA)


def _interpretar(r, plataforma=None) -> str:
    """Exit code e linha ZELADOR têm de CONCORDAR — senão é inconclusivo (nunca prova). Na
    Stoa/Alpaclass a morte ainda precisa da `prova: positiva` da sonda tri-estado."""
    code = r.get("exit_code")
    res = (r.get("linha") or {}).get("resultado")
    if code == 0 and res == R_VIVA:
        return R_VIVA
    if code == 3 and res == R_MORTA:
        if _sem_prova_positiva(r, plataforma):
            return R_INCONCLUSIVA
        return R_MORTA
    if code == 5 and res in _RESULTADOS_5:
        return res
    return R_INCONCLUSIVA


def _registrar(espinha, o_que, por_que, **kw):
    if espinha is None:
        return
    try:
        espinha.registrar_decisao(o_que, por_que, origem="athena-local/zelador", **kw)
    except Exception:
        pass


def _avisar(alertas, metodo, *args) -> bool:
    """True = entregue (ou sem canal: nada a re-tentar). False = re-tenta no próximo ciclo."""
    fn = getattr(alertas, metodo, None)
    if not callable(fn):
        return False
    try:
        return fn(*args) is not False
    except Exception:
        log.exception("zelador: alerta %s falhou", metodo)
        return False


def _escrever_atomico(path, dados) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".sessoes-status.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# --- o agendador -----------------------------------------------------------------------------------

class Zelador:
    """O passe de zelo do loop doméstico. `passo` roda UMA vez por ciclo, DEPOIS da captura.
    O estado vive no status JSON (carregado na 1ª consulta, não no construtor: construir não
    toca disco) e sobrevive a restart, inclusive o dedup dos alertas."""

    def __init__(self, cursos, *, modo, status_path, cfg=None, portao=None, rng=None,
                 relogio=time.time):
        if modo not in (SECO, LIGADO):
            raise ValueError(f"modo inválido para o zelador: {modo!r}")
        self.modo = modo
        self.status_path = status_path
        self.cfg = cfg or ConfigZelador()
        self._portao = portao
        self._rng = rng or random.Random()
        self._relogio = relogio
        self._meta = {c.url: c for c in cursos}
        self.unidades = unidades_de(cursos)
        self._por_chave = {u.chave: u for u in self.unidades}
        self._estado = None
        self._alertas = {"logins_avisados": None, "preventivos_avisados": {}}
        self._iniciados = []                  # epochs dos zelos iniciados (teto por hora)
        self._adiado_ate = 0.0
        self._em_curso = set()                # chaves com zelo desta encarnação no ar
        self._decisao = {}
        self._cache_caminhos = {}

    def descrever(self) -> str:
        c = self.cfg
        portao = self._portao.descrever() if hasattr(self._portao, "descrever") else "sem portão"
        return (f"{len(self.unidades)} unidade(s); ociosa >= {c.ociosa_s / _H:g} h; intervalo "
                f"{c.intervalo_min_s / _H:g}–{c.intervalo_max_s / _H:g} h; preventivo "
                f"< {c.preventivo_s / _DIA:g} dia(s); máx {c.max_por_hora}/h; portão: {portao}; "
                f"status em {self.status_path}")

    # --- estado ------------------------------------------------------------------------------
    @property
    def estado(self):
        self._carregar()
        return self._estado

    def _sortear(self, plataforma) -> float:
        a, b = self.cfg.intervalo(plataforma)
        return self._rng.uniform(a, b)

    def _novo_st(self, u):
        return {"chave": u.chave, "conta": u.conta, "plataforma": u.plataforma, "url": u.url,
                "status": DESCONHECIDA, "resultado": None, "relogio": "desconhecido",
                "mortes_seguidas": 0, "inconclusivas_seguidas": 0, "detalhe": "",
                "_t": {"intervalo_s": self._sortear(u.plataforma)}}

    def _carregar(self):
        if self._estado is not None:
            return
        self._estado = {u.chave: self._novo_st(u) for u in self.unidades}
        try:
            with open(self.status_path, encoding="utf-8") as f:
                dados = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning("zelador: status ilegível em %s (%s) — começo do zero",
                        self.status_path, type(e).__name__)
            return
        if not isinstance(dados, dict):
            return
        for c in dados.get("contas") or []:
            if not isinstance(c, dict):
                continue
            st = self._estado.get(c.get("chave"))
            if st is None:
                continue
            if c.get("status") in _STATUS:
                st["status"] = c["status"]
            for k in ("resultado", "relogio"):
                if isinstance(c.get(k), str):
                    st[k] = c[k]
            for k in ("mortes_seguidas", "inconclusivas_seguidas"):
                if isinstance(c.get(k), int) and not isinstance(c.get(k), bool):
                    st[k] = c[k]
            st["detalhe"] = _detalhe_seguro(c.get("detalhe"))
            t = c.get("_t")
            if isinstance(t, dict):
                for k, v in t.items():
                    if k in _T_PERSISTIDO and (v is None or _num(v) is not None):
                        st["_t"][k] = v
        al = dados.get("alertas") if isinstance(dados.get("alertas"), dict) else {}
        avisados = al.get("logins_avisados")
        if isinstance(avisados, list) and avisados and all(isinstance(a, str) for a in avisados):
            self._alertas["logins_avisados"] = sorted(avisados)
        prev = al.get("preventivos_avisados")
        if isinstance(prev, dict):
            self._alertas["preventivos_avisados"] = {
                k: v for k, v in prev.items() if k in self._por_chave and _num(v) is not None}

    # --- o passo do ciclo ------------------------------------------------------------------------
    def passo(self, executor, estado_cursos, *, agora, alertas, espinha=None, ativos=None):
        """UM passe: colhe os zelos que terminaram, observa a atividade de captura, emite os
        alertas (ligado), escolhe e dispara no máximo UM zelo (ligado) e grava o status."""
        self._carregar()
        self._decisao = {}
        self._cache_caminhos = {}
        drenar = getattr(executor, "drenar_zelos", None)
        if callable(drenar):
            for r in drenar(agora) or []:
                try:
                    self._aplicar(r, executor, estado_cursos, agora, espinha)
                except Exception:
                    log.exception("zelador: falha ao aplicar o resultado de %s", r.get("chave"))
        self._observar_capturas(executor, agora)
        if self.modo == LIGADO:
            self._alertar(alertas, agora, espinha, executor)
        self._agendar(executor, agora, ativos, espinha, estado_cursos)
        try:
            self._gravar_status(agora)
        except Exception as e:
            log.warning("zelador: não gravei o status em %s (%s)", self.status_path,
                        type(e).__name__)

    # --- caminhos da conta (a marca do M4 e as parceiras) -------------------------------------------
    def _caminhos(self, executor, meta):
        """(perfil_abs, sessao_abs) com que o motor desta conta roda — do `_montar` do daemon."""
        chave = (meta.url,)
        if chave in self._cache_caminhos:
            return self._cache_caminhos[chave]
        try:
            env, cwd = executor.ambiente_de(meta)
        except Exception:
            self._cache_caminhos[chave] = (None, None)
            return (None, None)
        perfil = env.get("CHROME_USER_DATA_DIR") or ""
        spec = captura._PLATAFORMAS.get(meta.plataforma)
        sessao = ""
        if spec is not None and spec.session_env:
            sessao = env.get(spec.session_env) or ""
        if not sessao and meta.plataforma == "hotmart":
            sessao = env.get("SESSION_STATE_PATH") or env.get("SESSION_PATH") or ""
        sessao = sessao or _SESSAO_PADRAO.get(meta.plataforma, "")

        def absol(p):
            if not p:
                return None
            return os.path.normpath(p if os.path.isabs(p) else os.path.join(cwd, p))

        par = (absol(perfil), absol(sessao))
        self._cache_caminhos[chave] = par
        return par

    def _marca(self, u, executor) -> float:
        """A maior mtime entre o perfil e o arquivo de sessão da unidade (0 se nenhum). Serve
        SÓ para RE-SONDAR uma conta aguardando-humano ("alguém mexeu": pode ser o próprio zelo
        ou a captura de outro curso). NUNCA rearma a captura — isso é do `_carimbo`."""
        meta = self._meta.get(u.url)
        if meta is None:
            return 0.0
        m = 0.0
        for p in self._caminhos(executor, meta):
            if not p:
                continue
            try:
                m = max(m, os.path.getmtime(p))
            except OSError:
                pass
        return m

    def _carimbo(self, u, executor) -> float:
        """epoch do último reseed HUMANO concluído da conta (0 se nenhum): o carimbo que o
        `scripts/reseed.py` do motor grava com o lock nas mãos — a ÚNICA marca que o zelador
        aceita para rearmar a captura (o zelo e a captura nunca o produzem)."""
        fn = getattr(executor, "carimbo_reseed", None)
        if not callable(fn):
            return 0.0
        try:
            v = _num(fn(u.conta))
        except Exception:
            return 0.0
        return v or 0.0

    def _url_da_sonda(self, u, estado_cursos, ativos):
        """A URL com que o zelo prova a sessão da unidade (achado da revisão: era sempre a do
        1º curso do cadastro). Um curso da unidade que a captura PODE rodar neste ciclo
        (gate/controle), sem latch nenhum (um produto sem acesso responde 403, que a sonda
        do Hotmart lê como morte — a conta inteira viraria "login necessário") e sem bench
        de exit-5 (URL que o motor não abre) — o de captura limpa mais recente; empate, a
        ordem do cadastro. Sem nenhum assim: um curso travado SÓ por reseed (a conta inteira
        morta — é justamente o zelo que prova o relogin, M4). None = nada sondável."""
        est = estado_cursos if isinstance(estado_cursos, dict) else {}
        limpos, so_reseed = [], []
        for i, url in enumerate(u.cursos):
            if url not in self._meta or (ativos is not None and url not in ativos):
                continue
            st = est.get(url) if isinstance(est.get(url), dict) else {}
            if st.get("benched_exit5"):
                continue
            if st.get("irredutivel"):
                if st.get("ultima_causa") == "escalar_reseed":
                    so_reseed.append(url)
                continue
            limpos.append((-(_num(st.get("_saida_limpa_ciclo")) or 0.0), i, url))
        if limpos:
            return min(limpos)[2]
        return so_reseed[0] if so_reseed else None

    def _parceiras(self, u, executor):
        """Contas (de QUALQUER curso do cadastro) que partilham o perfil ou o arquivo de
        sessão desta unidade — o zelo segura o lock de todas (dois escritores do mesmo
        arquivo = login perdido)."""
        meta = self._meta.get(u.url)
        if meta is None:
            return []
        perfil, sessao = self._caminhos(executor, meta)
        out = set()
        for outra in self._meta.values():
            if str(outra.conta) == u.conta:
                continue
            p2, s2 = self._caminhos(executor, outra)
            if (perfil and p2 == perfil) or (sessao and s2 == sessao):
                out.add(str(outra.conta))
        return sorted(out)

    # --- aplicar um resultado ---------------------------------------------------------------------
    def _aplicar(self, r, executor, estado_cursos, agora, espinha):
        u = self._por_chave.get(r.get("chave"))
        if u is None:
            return
        self._em_curso.discard(u.chave)
        st = self._estado[u.chave]
        t = st["_t"]
        resultado = _interpretar(r, u.plataforma)
        linha = r.get("linha") or {}
        st["resultado"] = resultado
        if r.get("watchdog"):
            st["detalhe"] = "watchdog"
        elif not linha:
            st["detalhe"] = "sem linha do zelador"
        elif _sem_prova_positiva(r, u.plataforma):
            st["detalhe"] = "morte sem prova positiva"
        else:
            st["detalhe"] = _detalhe_seguro(linha.get("detalhe"))
        exp = _num(linha.get("expira_em")) if resultado in (R_VIVA, R_MORTA) else None
        cfg = self.cfg

        if resultado == R_VIVA:
            estava = st["status"]
            st["status"] = VIVA
            st["mortes_seguidas"] = 0
            st["inconclusivas_seguidas"] = 0
            t["provado_ts"] = agora
            for k in ("primeira_morte_ts", "morte_ts", "marca_na_morte"):
                t.pop(k, None)
            if estava == AGUARDANDO:
                t.pop("expira_prova_ts", None)             # login novo: o relógio recomeça
            self._classificar_relogio(st, exp)
            t["intervalo_s"] = self._sortear(u.plataforma)
            t["proximo_ts"] = agora + t["intervalo_s"]
            self._rearmar(u, estado_cursos, self._carimbo(u, executor), espinha)
            if estava in (AGUARDANDO, SUSPEITA):
                _registrar(espinha, f"sessão de {u.chave} viva de novo",
                           "o zelador provou pela sonda da plataforma", reversivel=True,
                           plataforma=u.plataforma, fonte="deterministico")
            return

        if resultado == R_MORTA:
            st["mortes_seguidas"] += 1
            st["inconclusivas_seguidas"] = 0
            if exp is not None:
                t["expira_ts"] = exp
            fraca = u.plataforma in PROVA_FRACA
            if fraca and st["status"] not in (SUSPEITA, AGUARDANDO):
                st["status"] = SUSPEITA
                t["primeira_morte_ts"] = agora
                t["proximo_ts"] = agora + cfg.confirmacao_s
                return
            if fraca and st["status"] == SUSPEITA and \
                    agora - float(t.get("primeira_morte_ts") or agora) < cfg.confirmacao_s:
                t["proximo_ts"] = float(t["primeira_morte_ts"]) + cfg.confirmacao_s
                return
            novo = st["status"] != AGUARDANDO
            st["status"] = AGUARDANDO
            t["morte_ts"] = agora
            t["marca_na_morte"] = self._marca(u, executor)  # DEPOIS do zelo (ele mexe no perfil)
            t.pop("proximo_ts", None)
            if novo:
                _registrar(espinha, f"sessão de {u.chave} morta — aguardando login humano",
                           "morte PROVADA pela sonda da plataforma (zelador); paro de zelar "
                           "a conta até a sessão ser mexida", tipo="escalada",
                           reversivel=True, escalada=True, trava="anti-ban",
                           plataforma=u.plataforma, fonte="deterministico")
            return

        if resultado in _RESULTADOS_5:
            espera = cfg.ocupado_s if resultado == R_OCUPADO else float(t["intervalo_s"])
            t["proximo_ts"] = agora + espera
            return

        n = st["inconclusivas_seguidas"] = int(st.get("inconclusivas_seguidas") or 0) + 1
        t["proximo_ts"] = agora + min(cfg.backoff_base_s * 2 ** (n - 1), float(t["intervalo_s"]))

    def _classificar_relogio(self, st, exp):
        """DURO = o relógio NÃO avançou entre duas provas de vida (o uso não o renova: vai
        vencer e só o humano resolve); RENOVÁVEL = avançou (o uso rotaciona). Uma observação
        só não classifica. Sem relógio legível => nenhum."""
        t = st["_t"]
        if exp is None:
            st["relogio"] = "nenhum"
            t.pop("expira_ts", None)
            t.pop("expira_prova_ts", None)
            return
        prev = _num(t.get("expira_prova_ts"))
        if prev is None:
            st["relogio"] = "desconhecido"
        elif exp > prev + 60:
            st["relogio"] = "renovavel"
        elif abs(exp - prev) <= 60:
            st["relogio"] = "duro"
        else:
            st["relogio"] = "desconhecido"
        t["expira_prova_ts"] = exp
        t["expira_ts"] = exp

    def _rearmar(self, u, estado_cursos, carimbo, espinha):
        """M4: o zelo provou viva E há um reseed HUMANO (o carimbo) POSTERIOR ao latch do curso
        -> tira o latch de RESEED. Latch posterior ao carimbo (a captura morreu de novo depois
        do login) fica: rearmar por ele reabriria o laço. Só o latch de reseed
        (`escalar_reseed`), nunca o bench de exit-5."""
        if not carimbo or not isinstance(estado_cursos, dict):
            return
        for url in u.cursos:
            st = estado_cursos.get(url)
            if not isinstance(st, dict):
                continue
            if not (st.get("irredutivel") and st.get("ultima_causa") == "escalar_reseed"
                    and not st.get("benched_exit5")):
                continue
            if float(carimbo) <= float(st.get("_morte_ciclo") or 0):
                continue                                   # ninguém relogou depois do latch
            st.pop("irredutivel", None)
            st.pop("esgotado_avisado", None)
            st["fase"] = captura.FASE_NOVO
            _registrar(espinha, f"rearmei a captura de {url}",
                       "a sessão foi relogada depois do latch e o zelador provou viva "
                       "pela sonda da plataforma (M4)", reversivel=True, curso=url,
                       plataforma=u.plataforma, fonte="deterministico")

    # --- atividade de captura -----------------------------------------------------------------------
    def _observar_capturas(self, executor, agora):
        vivas = {}
        for u in self.unidades:
            conta = u.conta
            if conta not in vivas:
                viva = False
                try:
                    viva = bool(executor.captura_viva(conta))
                except Exception:
                    pass
                saida = None
                try:
                    saida = executor.ultima_saida_captura(conta)
                except Exception:
                    pass
                vivas[conta] = (viva, _num(saida))
            viva, saida = vivas[conta]
            t = self._estado[u.chave]["_t"]
            atual = _num(t.get("ultima_captura_ts"))
            if viva:
                t["ultima_captura_ts"] = agora
            elif saida is not None and (atual is None or saida > atual):
                t["ultima_captura_ts"] = saida

    # --- alertas -------------------------------------------------------------------------------------
    def _alertar(self, alertas, agora, espinha, executor=None):
        stoa_dir = None
        fn = getattr(executor, "motor_dir_de", None)
        if callable(fn):
            try:
                stoa_dir = fn("stoa") or None
            except Exception:
                stoa_dir = None

        def comando(alvos):
            return comando_reseed(alvos, motor_dir_stoa=stoa_dir)

        alvos = sorted({u.alvo for u in self.unidades
                        if self._estado[u.chave]["status"] == AGUARDANDO})
        if not alvos:
            self._alertas["logins_avisados"] = None        # reset: a próxima morte alerta
        elif alvos != self._alertas["logins_avisados"]:
            if _avisar(alertas, "logins_pendentes", alvos, comando(alvos)):
                self._alertas["logins_avisados"] = alvos
        for u in self.unidades:
            st = self._estado[u.chave]
            exp = _num(st["_t"].get("expira_ts"))
            if st["status"] == AGUARDANDO or st["relogio"] != "duro" or exp is None:
                continue
            if exp - agora >= self.cfg.preventivo_s:
                continue
            if self._alertas["preventivos_avisados"].get(u.chave) == exp:
                continue                                   # dedup pelo VALOR do relógio
            quando = _iso(exp).replace("T", " ")[:16] if _iso(exp) else "?"
            if _avisar(alertas, "sessao_vence", u.alvo, quando, (exp - agora) / _DIA,
                       comando([u.alvo])):
                self._alertas["preventivos_avisados"][u.chave] = exp
                _registrar(espinha, f"alertei vencimento da sessão de {u.chave}",
                           f"relógio duro vence em {quando}", reversivel=True,
                           plataforma=u.plataforma, fonte="deterministico")

    # --- escolher e disparar ---------------------------------------------------------------------------
    def _candidata(self, u, executor, agora):
        """(é_candidata, urgente, referencia, decisao_se_nao)."""
        st = self._estado[u.chave]
        t = st["_t"]
        if u.chave in self._em_curso:
            return (False, False, 0, "zelando")
        carimbo = self._carimbo(u, executor)
        humano = carimbo > (_num(t.get("carimbo_visto")) or 0.0)
        if st["status"] == AGUARDANDO:
            marca = self._marca(u, executor)
            if humano or marca > float(t.get("marca_na_morte") or 0) + 1:
                return (True, True, float(t.get("morte_ts") or 0), "")
            return (False, False, 0, "aguardando-humano")
        if humano:
            # login humano NOVO (carimbo do reseed): prova JÁ — é o zelo viva que rearma a
            # captura travada (M4); esperar o intervalo deixaria a captura parada horas.
            return (True, True, carimbo, "")
        prox = _num(t.get("proximo_ts"))
        if prox is not None and agora < prox:
            return (False, False, 0, "agendada")
        ultima = _num(t.get("ultima_captura_ts"))
        if ultima is not None and agora - ultima < self.cfg.ociosa_s:
            return (False, False, 0, "recente")
        ref = prox if prox is not None else (ultima or 0.0)
        return (True, False, ref, "")

    def _agendar(self, executor, agora, ativos, espinha, estado_cursos=None):
        candidatas = []
        urls = {}
        for u in self.unidades:
            if ativos is not None and not (set(u.cursos) & set(ativos)):
                self._decisao[u.chave] = "fora-do-gate"
                continue
            ok, urgente, ref, dec = self._candidata(u, executor, agora)
            if not ok:
                self._decisao[u.chave] = dec
                continue
            url = self._url_da_sonda(u, estado_cursos, ativos)
            if url is None:
                self._decisao[u.chave] = "sem-curso-sondavel"
                continue
            urls[u.chave] = url
            candidatas.append((0 if urgente else 1, ref, u.chave, u))
        candidatas.sort(key=lambda x: x[:3])
        if not candidatas:
            return

        if self.modo == SECO:
            for _, _, _, u in candidatas:
                contas = [u.conta] + self._parceiras(u, executor)
                livres = True
                try:
                    livres = executor.contas_livres(contas)
                except Exception:
                    pass
                self._decisao[u.chave] = "zelaria-agora" if livres else "ocupada"
            return

        def marcar(dec):
            for _, _, chave, _u in candidatas:
                self._decisao.setdefault(chave, dec)

        try:
            em_curso = executor.zelo_em_curso()
        except Exception:
            em_curso = True                                # na dúvida, não abre outro
        if em_curso:
            return marcar("espera-outro-zelo")
        self._iniciados = [x for x in self._iniciados if agora - x < _H]
        if len(self._iniciados) >= self.cfg.max_por_hora:
            return marcar("teto-hora")
        if agora < self._adiado_ate:
            return marcar("adiada-carga")
        if self._portao is not None:
            try:
                motores = executor.motores_ativos()
            except Exception:
                motores = 0
            try:
                veto = self._portao.avaliar(motores)
            except Exception:
                veto = None
            if veto:
                self._adiado_ate = agora + self.cfg.adiar_s
                log.warning("zelador: zelo adiado %d min — %s", int(self.cfg.adiar_s // 60), veto)
                return marcar("adiada-carga")

        for _, _, _, u in candidatas:
            meta = self._meta.get(urls[u.chave])
            if meta is None:
                self._decisao[u.chave] = "sem-cadastro"
                continue
            carimbo = self._carimbo(u, executor)
            try:
                executor.disparar_zelo(u.chave, meta, parceiras=self._parceiras(u, executor),
                                       agora=agora)
            except captura.ContaOcupada:
                self._decisao[u.chave] = "ocupada"
                continue
            except Exception as e:
                self._decisao[u.chave] = "erro-disparo"
                log.warning("zelador: não disparei o zelo de %s (%s)", u.chave, type(e).__name__)
                continue
            t = self._estado[u.chave]["_t"]
            self._iniciados.append(agora)
            self._em_curso.add(u.chave)
            t["ultimo_zelo_ts"] = agora
            t["proximo_ts"] = agora + self.cfg.adiar_s     # guarda: resultado perdido num restart
            t["carimbo_visto"] = carimbo                   # este login humano já tem o seu zelo
            self._decisao[u.chave] = "zelando"
            break
        marcar("espera-outro-zelo")

    # --- status (sem credencial, escrita atômica) ------------------------------------------------------
    def _gravar_status(self, agora):
        contas = []
        for u in self.unidades:
            st = self._estado[u.chave]
            t = st["_t"]
            exp = _num(t.get("expira_ts"))
            contas.append({
                "chave": u.chave, "conta": u.conta, "plataforma": u.plataforma, "url": u.url,
                "status": st["status"], "resultado": st["resultado"],
                "provado_em": _iso(t.get("provado_ts")), "expira_em": _iso(exp),
                "vence_em_h": None if exp is None else int(round((exp - agora) / _H)),
                "relogio": st["relogio"], "mortes_seguidas": st["mortes_seguidas"],
                "inconclusivas_seguidas": st["inconclusivas_seguidas"],
                "detalhe": _detalhe_seguro(st["detalhe"]),
                "decisao": self._decisao.get(u.chave, ""),
                "proximo_em": _iso(t.get("proximo_ts")),
                "ultima_captura_em": _iso(t.get("ultima_captura_ts")),
                "_t": {k: v for k, v in t.items() if k in _T_PERSISTIDO},
            })
        dados = {"gerado_em": _iso(agora), "modo": self.modo, "contas": contas,
                 "alertas": {"logins_avisados": list(self._alertas["logins_avisados"] or []),
                             "preventivos_avisados": dict(self._alertas["preventivos_avisados"])}}
        _escrever_atomico(self.status_path, dados)


def zelador_do_ambiente(cursos, env=None, status_path=None):
    """O Zelador do `main()`, ou None (DESLIGADO — o default). Só GUARDA o caminho do status
    (ATHENA_SESSOES_STATUS ou ~/.athena-local/sessoes-status.json); nada é lido nem escrito
    até o 1º passo."""
    env = os.environ if env is None else env
    modo = modo_do_ambiente(env)
    if modo == DESLIGADO:
        return None
    return Zelador(cursos, modo=modo,
                   status_path=status_path or (env.get(STATUS_ENV) or "").strip() or STATUS_PADRAO,
                   cfg=ConfigZelador.do_ambiente(env), portao=portao_do_ambiente(env))
