"""ENTRYPOINT DOMÉSTICO da Athena — o loop que roda NO MAC (`python -m maestro.athena_local`).

Decisão de arquitetura (INVIOLÁVEL): a Athena roda DOMÉSTICA no Mac, NÃO na VPS. O
executor de captura, aqui, é o `captura.LocalExecutor` (chama o MOTOR DIRETO como
subprocesso), NÃO o `FilaExecutor` (que enfileira pro worker da VPS). Este módulo é o
`main.py` do modelo doméstico: monta o executor local + a contagem-verdade do Notion
LOCAL e roda o mesmo LOOP-DONO já testado.

REUSO (não reimplementação): a COORDENAÇÃO é a MESMA `orquestrador.orquestrar_captura`
do modelo VPS — completude-por-Notion, vigília de STALL, uma-passada-por-ciclo,
fail-closed quando uma passada estoura. O que muda é só a `passada_fn` (o EXECUTOR):
de `coordenar(executor=FilaExecutor)` para uma passada LOCAL enxuta sobre o
`LocalExecutor`.

=====================================================================================
INTEGRAÇÃO DAS 6 PARTES (never-stop + controle), FIADAS AQUI (ponto único de costura):
  P5 CONTROLE   — `controle.filtrar_cursos(cursos, path)` a cada ciclo: plataforma/conta
                  pausada em `controle.yaml` NÃO é RE-disparada (nunca mata processo vivo).
  P4 DISJUNTOR  — `disjuntor.pode_tentar/registrar_falha/registrar_sucesso(st, agora)`:
                  RECOZIMENTO (backoff re-armável 10min→1h→6h→24h) no gate de re-tentativa,
                  no lugar do teto permanente. Causa IRREDUTÍVEL (reseed/token) abre de vez.
  P3 AUTÓPSIA   — na MORTE de um curso, `vigia.autopsia(lock_dir, obitos)` colhe a evidência
                  (exit_code/stderr/flap) e `causa.classificar(obito)` decide a causa-raiz.
  P2 BATIMENTO  — `batimento.talvez_bater(voz, resumo, agora, ultimo)` (pulso de vida) +
     /ALERTAS     `alertas` typados na morte/flap/sessão. Fail-safe: mudo LOGA, não POSTA.
  P1 COMPOSIÇÃO — a costura em si: mapeia a decisão da causa em ação do disjuntor/alerta.

A costura vive TODA neste arquivo (o loop). Cada colaborador entra INJETADO; o DEFAULT é
um NULL-OBJECT que preserva EXATAMENTE o comportamento pré-integração — de modo que os
testes-invariante do loop rodam sem as 6 partes e o daemon degrada com graça. As
assinaturas dos null-objects BATEM com as dos módulos REAIS (não com contratos idealizados).
=====================================================================================

PENDENTE / o que ficou por LIGAR (honesto):
  - A lista de cursos vem de um YAML (ATHENA_LOCAL_CURSOS); a ponte para o cadastro do
    Painel/Notion não está ligada.
  - `total_esperado` (denominador da completude) vem do YAML; sem ele (0) o owner nunca
    conclui (fail-closed). Não há oráculo local de quantas aulas o curso tem.
  - AUTO-INGEST/SINTETIZADOR pós-captura (esteira downstream) não roda aqui.
  - STDERR na autópsia: o `_spawn_popen` tee'a stdout+stderr do motor por CONTA em
    ~/.athena-local/motor-logs/<hash>.err (truncado a cada disparo). O filho DESTA
    encarnação chega à autópsia com exit_code + a CAUDA COPIADA NO REAP (`stderr_tail`:
    o reap roda até no meio da passada, e a conta podia ser relançada — truncando o .err
    — antes da autópsia do ciclo seguinte; incidente 10/09). A morte SÓ-DE-LOCK (captura
    de uma encarnação anterior, sem exit code) lê o mesmo .err via `stderr_path_de`. O
    exit code de uma órfã continua NÃO observável: a causa só a dá por limpa pelo resumo
    final do motor na cauda, e por morta no reinício do Mac pelo lock anterior ao boot.
"""
import asyncio
import json
import logging
import os
import time

from maestro import adaptador_pipeline, orquestrador
from maestro.adaptadores import captura
from maestro.playbook import Acao
from maestro.rotulo import rotulo_seguro
from maestro.sentinela import Problema
from maestro.vigia import _FLAP_MIN as _FLAP_MIN_PADRAO

log = logging.getLogger("athena.local")

# Fase por-curso (reusa as do adaptador de captura — mesmo vocabulário de máquina de
# estados; só as fases NOVO/CAPTURANDO/CONCLUIDO importam no doméstico).
FASE_NOVO = captura.FASE_NOVO
FASE_CAPTURANDO = captura.FASE_CAPTURANDO
FASE_CONCLUIDO = captura.FASE_CONCLUIDO

# Causa que trava o curso de VEZ (irredutível): SÓ a SESSÃO morta. Uma sessão morta
# é a SUPERFÍCIE DE BAN — martelá-la deslogado é o caminho do banimento da conta paga,
# então o disjuntor abre até o humano refazer o login HEADED (reseed).
#
# `escalar_token` (credencial de API) SAIU daqui DE PROPÓSITO: a chave é de uma API
# DOWNSTREAM (Groq/Anthropic), NÃO da plataforma raspada — travar de vez NÃO protege
# de ban NENHUM, só condena o curso a parar até um humano agir. Um blip de auth
# momentâneo latchava a captura permanentemente (ver incidente Stoa 21/07). Agora o
# token vai pro ramo de BACKOFF (recozimento) com alerta: um blip se cura sozinho, e
# uma chave de fato revogada re-tenta com atraso crescente (10min→...→24h) + alerta,
# NUNCA um latch permanente. Anti-ban intacto: reseed continua irredutível.
_CAUSAS_IRREDUTIVEIS = ("escalar_reseed",)

# BENCH do exit-5 (anti-flap): exit 5 = sonda de sessão INCONCLUSIVA / erro de infra
# (contrato do motor — NÃO é sessão morta). Uma URL MALFORMADA no YAML (ex.:
# …/products/X/agent) torna a sonda inconclusiva PARA SEMPRE: cada disparo morre
# exit-5, a causa diz `relancar`, e o curso flapa infinito. Após N mortes exit-5
# SEGUIDAS no MESMO curso (mesma URL — o `st` é chaveado por URL), o curso é
# BENCHED (irredutível: o disjuntor para de re-tentar SÓ este curso) + ALERTA ESSENCIAL
# com a URL (dedup por curso; r6 — antes só-log, e o curso parava calado).
# INVIOLÁVEL ANTI-BAN: o bench NÃO é reseed nem relogin (sonda inconclusiva
# ≠ sessão morta confirmada — esta segue a escalada normal de reseed, que tem
# precedência sobre o bench); e é POR-CURSO: os demais cursos, inclusive da MESMA
# conta, seguem. N=3 espelha o _FLAP_MIN (1 exit-5 é transitório comum de rede;
# 3 seguidos — espaçados pelo recozimento do backoff — não é blip, é a URL).
# A contagem zera em: saída limpa, avanço real no Notion (que também DESBENCHA:
# se outra via produziu progresso, never-stop reavalia), ou morte de outra causa.
_EXIT_SONDA_INCONCLUSIVA = 5           # contrato do motor (ver causa.py)
_BENCH_EXIT5_MIN = int(os.getenv("ATHENA_BENCH_EXIT5_MIN", "3"))

# COOLDOWN de curso QUIESCIDO por SAÍDA LIMPA: quando o motor sai LIMPO (exit 0) sem
# produzir nada novo (Notion não avançou), o curso está concluído/sem-pendência para o
# denominador que temos. Em vez de re-spawnar a cada ciclo (o que floodava o vigia com
# saídas-limpas e queimava sessão à toa), o curso entra num COOLDOWN: fica quieto por
# esta janela e só é reavaliado ao expirar. NÃO é permanente (never-stop): se novo
# conteúdo aparecer, um ciclo pós-cooldown o retoma; e QUALQUER avanço real no Notion —
# ou pendência capturável NOVA no tracker (acima do baseline do arme; ex.: reseed) —
# limpa o cooldown na hora. Default 6h; env ATHENA_COOLDOWN_CONCLUIDO_S calibra.
_COOLDOWN_SAIDA_LIMPA_S = float(os.getenv("ATHENA_COOLDOWN_CONCLUIDO_S", "21600"))  # 6h

# COOLDOWN de curso SEM PENDÊNCIA CAPTURÁVEL (higiene 2): o tracker prova que só restam
# aulas TERMINAIS (no_notion/sem_conteudo/falhou…), mesmo com o Notion < total (aulas
# terminais nunca chegam ao Notion). É "essencialmente pronto" — não re-disparar NEM
# escalar exit-4 a cada ciclo (falso-positivo invistodireito 533/547). Cooldown LONGO
# (mais que a saída-limpa: aqui o tracker PROVA que não há trabalho, então revisitar é
# ainda mais raro). QUALQUER avanço no Notion / pendência nova limpa na hora (never-stop).
# INVARIANTE: pendência REAL (parede/throttle => aula pendente/in-flight) NÃO cai aqui.
_COOLDOWN_SEM_PENDENCIA_S = float(os.getenv("ATHENA_COOLDOWN_SEM_PENDENCIA_S", "86400"))  # 24h

# PORTÃO DE CARGA (maestro.carga; incidente 10/09): o LocalExecutor ADIA o disparo quando o
# Mac está sobrecarregado. Adiar é auto-tratado — o aviso agregado por ciclo fica SÓ-LOG;
# se a sobrecarga PERSISTIR por esta janela (nada sendo capturado), o aviso vira ESSENCIAL
# (1 ping por janela de dedup do Alertas) + 1 escalada na espinha por episódio. 0 = nunca
# escala (só-log sempre). Default 1h.
_CARGA_ALERTA_S = float(os.getenv("ATHENA_CARGA_ALERTA_S", "3600"))

# EPISÓDIO DE SOBRECARGA EM DISCO (achado r6): o `desde` do episódio vai para um arquivo
# pequeno (ATHENA_CARGA_EPISODIO_PATH, default ~/.athena-local/carga_episodio.json) e o
# `rodar` o retoma no boot — reinícios curtos (vigia externo, launchd) não zeram mais o
# relógio do aviso ESSENCIAL. LACUNA: se a última observação de adiamento ficou mais longe
# que isto do boot, o loop esteve fora tempo demais para chamar de "o mesmo episódio" —
# recomeça do zero (honesto). Default 30 min (o vigia externo mata após 900 s sem pulso).
_CARGA_EPISODIO_LACUNA_S = float(os.getenv("ATHENA_CARGA_EPISODIO_LACUNA_S", "1800"))


def _env_int(nome, padrao, *, minimo=None):
    """Env numérica TOLERANTE: valor ausente/vazio/ilegível => `padrao` (com WARNING no
    caso ilegível); abaixo de `minimo` => `minimo` (com WARNING). Uma variável de ambiente
    mal digitada nunca pode impedir o daemon de subir."""
    bruto = os.getenv(nome)
    if bruto is None or not str(bruto).strip():
        valor = int(padrao)
    else:
        try:
            valor = int(str(bruto).strip())
        except (TypeError, ValueError):
            log.warning("%s=%r não é inteiro — usando o default %s", nome, bruto, padrao)
            valor = int(padrao)
    if minimo is not None and valor < minimo:
        log.warning("%s=%s abaixo do mínimo %s — usando %s", nome, valor, minimo, minimo)
        valor = int(minimo)
    return valor


# ESTADO DO DISJUNTOR EM DISCO (achado r15) — ANTI-BAN.
#
# O `estado` por-curso nascia `{}` a cada `rodar()`. Como o daemon é reiniciado pelo
# vigia externo / launchd / reboot do Mac, TODO reinício zerava a escada de backoff
# (600s → 1h → 6h → 24h) e o cooldown de saída-limpa/sem-pendência: um curso que o
# disjuntor tinha mandado esperar 24 h voltava a ser MARTELADO no primeiro ciclo da
# encarnação nova. Justamente o oposto do que a escada existe para fazer — e a
# martelada acontece contra a plataforma, que é a superfície de ban.
#
# Agora um JSON pequeno (uma entrada por curso, ~6 números) é gravado ao fim de cada
# ciclo e RESTAURADO no boot. Mesmo padrão do episódio de sobrecarga: troca atômica
# (tmp + os.replace), best-effort na escrita (falhar nunca derruba o loop) e arquivo
# ilegível/corrompido é DESCARTADO com WARNING (nunca derruba o boot).
#
# O QUE É PERSISTIDO (lista BRANCA — nada fora dela atravessa o reinício):
#   disj_falhas, disj_bloqueado_ate   a escada do recozimento
#   cooldown_ate, _pend_no_cooldown   a janela de quiescido/sem-pendência + baseline
#   ultimo_no_notion                  a régua do AVANÇO (sem ela, o 1º ciclo pós-boot
#                                     não reconhece progresso e não re-arma o disjuntor)
#
# O QUE NÃO É (de propósito): `irredutivel`, `benched_exit5`, `exit5_seguidas`, `fase`,
# `ultima_causa` e os latches de alerta. São LATCHES cujo ÚNICO destravamento hoje, com
# o zelador DESLIGADO por padrão, é o reinício do daemon (o `Zelador._rearmar` existe,
# mas só roda com ATHENA_ZELADOR_ATIVO). Persistir um latch de reseed sem ter quem o
# destrave transformaria "curso travado até o próximo boot" em "curso travado PARA
# SEMPRE, calado" — um bug pior que o que estamos consertando. Quando o zelador estiver
# ligado por padrão, persistir `irredutivel` vira uma decisão separada e consciente.
# (O FLAP não entra aqui porque já é durável: `vigia._contar_flaps_anteriores` conta os
# JSONs de autópsia em disco na janela, não um contador em memória.)
_ESTADO_CURSOS_CHAVES = ("disj_falhas", "disj_bloqueado_ate", "cooldown_ate",
                         "_pend_no_cooldown", "ultimo_no_notion")
# TETO DE SANIDADE do futuro: um valor corrompido (ou um relógio que andou para trás)
# não pode bloquear um curso por anos. Qualquer instante além de `agora + isto` é
# DESCARTADO na leitura — 24h é o teto da escada e 24h é o cooldown sem-pendência, mais
# uma folga generosa.
_ESTADO_FUTURO_MAX_S = 2 * 86400.0


# ---------------------------------------------------------------------------
# NULL-OBJECTS (defaults) — assinaturas IDÊNTICAS às dos módulos reais (P3/P4/P5/P2),
# de modo que injetar o módulo real ou o default é transparente para o loop.
# ---------------------------------------------------------------------------
class _ControleNulo:
    """DEFAULT P5: nada pausado (a lista inteira flui). Assinatura == controle.filtrar_cursos."""
    def filtrar_cursos(self, cursos, path=None):
        return list(cursos)


class _DisjuntorTeto:
    """DEFAULT P4: o TETO FIXO de tentativas pré-integração (SEM recozimento). Assinaturas
    == disjuntor.{pode_tentar,registrar_falha,registrar_sucesso}(st, agora). `pode_tentar`
    abre enquanto tentativas < max e fecha no teto; `registrar_falha/sucesso` são no-op
    (o contador `tentativas` sobe no disparo). O disjuntor REAL (backoff re-armável)
    substitui isto quando injetado — sem tocar na fiação."""
    def __init__(self, max_tentativas):
        self._max = max_tentativas

    def pode_tentar(self, st, agora):
        if st.get("irredutivel"):
            return False
        return st.get("tentativas", 0) < self._max

    def registrar_falha(self, st, agora):
        return None

    def registrar_sucesso(self, st):
        return None


class _DisjuntorRecozido:
    """P4 de PRODUÇÃO: o módulo `maestro.disjuntor` (recozimento re-armável) LIGADO ao
    limiar configurado — é ele que o `main` injeta.

    POR QUE ELE EXISTE (achado r15): `ATHENA_MAX_TENTATIVAS` era CÓDIGO MORTO. O `main`
    lia a env, passava `max_tentativas=` ao `rodar`/`ciclo_local`, e lá ela só servia
    para construir o `_DisjuntorTeto` — o fallback que NUNCA é alcançado, porque o `main`
    sempre injeta o disjuntor real. O limiar REAL era o `LIMIAR_PADRAO = 3` hardcoded do
    módulo, já que as chamadas eram `disjuntor.pode_tentar(st, agora)` sem `limiar=`.
    Isto é: a env prometia calibração e não fazia NADA — e o docstring do disjuntor
    dizia "injetável para calibração por plataforma". Este adaptador CUMPRE a promessa:
    fixa o limiar UMA vez, no boot, e o repassa em toda chamada. Nada muda no caminho
    quente (o loop segue chamando `pode_tentar(st, agora)`), e com a env ausente o
    limiar é o mesmo 3 de sempre: ZERO mudança de comportamento em produção hoje."""

    def __init__(self, modulo, limiar):
        self._m = modulo
        self.limiar = int(limiar)

    def pode_tentar(self, st, agora):
        return self._m.pode_tentar(st, agora, limiar=self.limiar)

    def registrar_falha(self, st, agora):
        return self._m.registrar_falha(st, agora, limiar=self.limiar)

    def registrar_sucesso(self, st):
        return self._m.registrar_sucesso(st)


class _VigiaNulo:
    """DEFAULT P3: sem autópsia (nenhum óbito a colher). Assinatura == vigia.autopsia."""
    def autopsia(self, lock_dir, stderr_por_conta, **kw):
        return []


class _CausaNula:
    """DEFAULT P3: nunca chamada (o vigia nulo não devolve óbitos)."""
    def classificar(self, obito, llm=None):
        return None


class _BatimentoNulo:
    """DEFAULT P2: relógio intacto (nenhum pulso). Assinatura == batimento.talvez_bater."""
    def talvez_bater(self, voz, resumo_fn, agora, ultimo, intervalo=1800):
        return ultimo


class _AlertasNulo:
    """DEFAULT P2: alertas typados viram no-op (o loop segue sem canal de supervisão).
    Aceita os kwargs do gate de essencialidade (`essencial`/`chave`) do Alertas real."""
    def captura_morreu(self, plataforma, motivo, **kw):
        return None

    def sessao_expirada(self, plataforma, **kw):
        return None

    def curso_concluido(self, plataforma, curso, n, **kw):
        return None

    def maquina_sobrecarregada(self, motivo, **kw):
        return None

    def logins_pendentes(self, alvos, comando, **kw):
        return True                                        # sem canal: nada a re-tentar

    def sessao_vence(self, alvo, quando, dias, comando, **kw):
        return True


class _EspinhaNula:
    """DEFAULT F4-a (espinha): SEM log de decisões — o comportamento pré-integração.
    Assinatura == `decisoes.registrar_decisao` (aceita QUALQUER kwarg do contrato,
    inclusive `sistema=`). Os testes-invariante do loop rodam sem a espinha; o
    `main()` injeta o módulo `decisoes` real. Nunca levanta (é no-op)."""
    def registrar_decisao(self, o_que, por_que, **kw):
        return None


def _registrar(espinha, o_que, por_que, **kw):
    """Chama a espinha À PROVA DE FALHAS: um erro no LOG jamais derruba o loop
    (mesmo contrato do pulso/batimento/alertas). A espinha REAL
    (`decisoes.registrar_decisao`) já é fail-safe por construção; este wrapper
    protege TAMBÉM contra uma espinha INJETADA defeituosa — o null-object garante
    o default, o wrapper garante que observabilidade nunca é quem mata o loop."""
    try:
        espinha.registrar_decisao(o_que, por_que, **kw)
    except Exception:
        pass


# Proxy de custo do diagnóstico `claude -p` (E14): o seam `causa.seam_claude_p`
# NÃO devolve usage, então o custo é PRESUMIDO por um valor fixo e SEMPRE
# rotulado `medido=False` — a regra inviolável nº2 da espinha (nunca somar
# presumido com medido). Uma classificação de causa-raiz é 1 chamada headless
# curta; o proxy é conservador e serve só ao teto/observabilidade, jamais à fatura.
_CUSTO_PROXY_CLAUDE_P_USD = float(os.getenv("ATHENA_CUSTO_PROXY_CLAUDE_P_USD", "0.02"))


def _safe_ativo(executor, curso_url) -> bool:
    try:
        return bool(executor.curso_ativo(curso_url))
    except Exception:
        return False


def _aguardando_autopsia(executor, curso_url) -> bool:
    """A conta do curso tem óbito colhido e ainda não autopsiado? Fail-open: executor sem
    a sonda (dublês antigos / FilaExecutor) ou sonda que levanta => False (comportamento de
    sempre — o fallback da passada segue cobrindo a morte)."""
    sonda = getattr(executor, "aguardando_autopsia", None)
    if not callable(sonda):
        return False
    try:
        return bool(sonda(curso_url))
    except Exception:
        return False


def _texto_bench(curso, n5) -> str:
    """Motivo do alerta ESSENCIAL de bench exit-5. O que o dono precisa: QUAL curso parou,
    POR QUÊ (sonda inconclusiva em série = provável URL malformada / adaptador) e O QUE
    fazer (conferir a URL no YAML). Nenhuma frase de sessão morta nem de credencial: o
    texto (e o rótulo do curso, via `rotulo_seguro`) nunca casa `_RE_SESSAO`/`_RE_TOKEN`."""
    return (f"BENCH exit-5: {n5} sondas inconclusivas seguidas em "
            f"{rotulo_seguro(curso)} — curso PARADO. Ação: confira a URL desse curso no "
            f"YAML (provável malformada) ou o adaptador da plataforma. Não é o acesso da "
            f"conta: os demais cursos dela seguem; avanço real no Notion reabre este")


def _plataforma_de(curso_url, meta_por_curso) -> str:
    """Rótulo de plataforma para os alertas typados. Prefere a `plataforma` do CursoLocal;
    cai na inferência por URL; por fim, a própria URL (nunca vazio)."""
    meta = (meta_por_curso or {}).get(curso_url)
    if meta is not None and getattr(meta, "plataforma", ""):
        return meta.plataforma
    return adaptador_pipeline.plataforma_de_url(curso_url) or curso_url


# ---------------------------------------------------------------------------
# AUTÓPSIA + CAUSA-RAIZ (P3) fiadas na MORTE de um curso -> decisão do disjuntor (P4)
# ---------------------------------------------------------------------------
def _aplicar_decisao(curso, st, obito, decisao, *, disjuntor, alertas, agora,
                     meta_por_curso, flap_min, espinha=None):
    """Traduz a `Decisao` da causa-raiz em ação de never-stop sobre o `st` do curso.

      transitório (relancar/aguardar_backoff/None) -> `registrar_falha` avança o backoff
        do disjuntor; o curso volta a NOVO e re-tenta SOB o gate (recozimento, não martelo).
      IRREDUTÍVEL (escalar_reseed) -> marca `st['irredutivel']`: o disjuntor abre de vez
        (não re-tenta) e ALERTA o humano. SÓ a sessão morta (superfície de ban) latcha.
      TOKEN (escalar_token) -> back off + ALERTA "troque o token": o disjuntor RECOZE e
        re-tenta (a chave é API downstream, não a plataforma — travar de vez não evita ban).
      desconhecida (escalar_humano) -> back off + ALERTA (fail-closed: chama o dono).
      FLAP (>= flap_min mortes na janela) -> ALERTA de captura morrendo em loop, seja qual
        for a causa.
    """
    acao = getattr(decisao, "acao", None)
    plat = _plataforma_de(curso, meta_por_curso)
    st["ultima_causa"] = acao
    espinha = espinha or _EspinhaNula()
    fonte_causa = getattr(decisao, "fonte", None) or "deterministico"
    motivo_causa = getattr(decisao, "motivo", "") or ""

    # SAÍDA LIMPA (exit 0 SEM assinatura alarmante): o motor rodou e saiu sem erro — NÃO é
    # morte. Exigimos DOIS sinais concordantes: exit_code == 0 E a causa classificada como
    # `aguardar_backoff` (o veredito da causa.py para saída limpa). Se o stderr denunciasse
    # sessão/token morta, a causa seria escalar_reseed/escalar_token e cairíamos no ramo de
    # MORTE REAL abaixo — anti-ban intacto (uma sessão morta que saiu 0 ainda escala). Aqui
    # NÃO se conta flap, NÃO se escala, NÃO se avança o backoff, NÃO se marca _morte_ciclo:
    # a passada decide cooldown/conclusão pela completude/avanço no Notion.
    # A captura ÓRFÃ que terminou LIMPA (causa: `sem_falha` + aguardar_backoff — exit
    # code não observável, a cauda do .err termina no resumo final do motor) recebe o
    # MESMO tratamento: se esta encarnação a tinha em CAPTURANDO ("ja_capturando" no
    # disparo), a passada arma o cooldown de concluído como num exit 0 real.
    sem_falha = bool(getattr(decisao, "sem_falha", False))
    if ((getattr(obito, "exit_code", None) == 0 or sem_falha)
            and acao == "aguardar_backoff"):
        st["_saida_limpa_ciclo"] = agora
        st.pop("exit5_seguidas", None)   # um run limpo prova a sonda sã: zera o bench
        return

    # MORTE SEM FALHA da captura (causa: `sem_falha` — reinício/desligamento do Mac
    # matou uma captura disparada ANTES do boot, sem outra causa na cauda): NÃO conta
    # falha no disjuntor, NÃO alerta (nem o essencial, nem o de flap — e o vigia não a
    # conta no flap das próximas) e o curso volta a NOVO: a passada re-avalia e
    # re-dispara sob o gate de sempre (never-stop). Voltar a NOVO também tira o curso do
    # FALLBACK de morte da passada (que só age em CAPTURANDO) — senão ele contaria a
    # falha que esta decisão acabou de NÃO contar.
    if sem_falha:
        _registrar(espinha, f"relanço {curso} sem contar falha",
                   motivo_causa or "morte sem falha da captura", reversivel=True,
                   curso=curso, plataforma=plat, fonte=fonte_causa,
                   origem="athena-local/causa")
        st["fase"] = FASE_NOVO
        return

    # MORTE REAL (não é saída limpa): um cooldown de 'concluído' herdado de um run limpo
    # anterior NÃO se aplica a um curso que acabou de MORRER — limpa-o para não atrasar a
    # escalada nem mascarar o disjuntor. (Anti-ban: a escalada de reseed abaixo é imediata.)
    st.pop("cooldown_ate", None)
    st["_morte_ciclo"] = agora                       # a passada NÃO re-trata esta morte

    flaps = int(getattr(obito, "flaps_na_janela", 0) or 0)
    if flaps >= flap_min:
        # SÓ-LOG por default (não-essencial): o flap re-tenta SOZINHO sob o
        # disjuntor — nenhuma ação do dono. Se a CAUSA pedir humano
        # (reseed/token/desconhecida), os ramos abaixo alertam essencial.
        alertas.captura_morreu(
            plat, f"FLAP: {flaps} mortes na janela (curso {curso}, causa={acao})")
        # E5: flapping detectado — escalada (a captura morre em loop, seja a causa
        # qual for). Latch de flap não existe (a janela já dedup por autópsia).
        _registrar(espinha, f"FLAP: {flaps} mortes na janela em {curso}",
                   f"captura morrendo em loop (causa={acao})", tipo="escalada",
                   reversivel=True, escalada=True, curso=curso, plataforma=plat,
                   fonte=fonte_causa, origem="athena-local/causa")

    if acao in _CAUSAS_IRREDUTIVEIS:      # só escalar_reseed: sessão morta = superfície de ban
        st["irredutivel"] = True
        st["esgotado_avisado"] = True                # o alerta typado abaixo já cobre
        st.pop("benched_exit5", None)                # o latch agora é de RESEED, não de bench
        alertas.sessao_expirada(plat)
        st["fase"] = FASE_NOVO
        # E1: sessão morta → curso TRAVADO até reseed humano (irredutível, anti-ban).
        _registrar(espinha, f"travei {curso} até reseed humano",
                   motivo_causa or "sessão morta (superfície de ban)",
                   reversivel=False, escalada=True, trava="anti-ban",
                   curso=curso, plataforma=plat, fonte=fonte_causa,
                   origem="athena-local/causa")
        return

    # BENCH do exit-5 (anti-flap; DEPOIS do ramo irredutível de propósito — uma
    # sessão DECLARADA morta escala reseed normal mesmo com exit 5, o bench nunca
    # a mascara). Conta mortes exit-5 SEGUIDAS deste curso; no limiar, bencha SÓ o
    # curso (irredutível) + alerta com a URL — sem reseed, sem relogin, e a conta
    # segue nos demais cursos. Morte de OUTRA causa zera a contagem (o bench é
    # para o exit-5 IDÊNTICO em série da URL malformada, não para azar misto).
    # A série exige a MESMA CAUSA CLASSIFICADA, não só o exit-code cru (review A1):
    # um exit-5 classificado `escalar_token` NÃO conta nem bencha — benchar por token
    # seria um latch SEM via de desbench (o curso nunca roda, o Notion nunca avança)
    # e engoliria o alerta "troque o token" da morte que bencha. `acao is None`
    # (classificador estourou) ainda conta: numa URL malformada real a série não
    # pode depender do classificador de pé. E morte SEM exit_code (óbito por PID
    # morto — ex.: lock órfão de encarnação/conta antiga com o mesmo course_url)
    # NÃO quebra a série (review A2): um fantasma sem veredito re-zeraria o
    # contador a cada ciclo e reabriria o flap infinito que este fix mata.
    exit5 = getattr(obito, "exit_code", None) == _EXIT_SONDA_INCONCLUSIVA
    if exit5 and (acao == "relancar" or acao is None):
        n5 = int(st.get("exit5_seguidas", 0) or 0) + 1
        st["exit5_seguidas"] = n5
        if n5 >= _BENCH_EXIT5_MIN:
            st["irredutivel"] = True                  # o disjuntor para SÓ este curso
            st["benched_exit5"] = True
            st["esgotado_avisado"] = True             # o alerta typado abaixo já cobre
            st["fase"] = FASE_NOVO
            # ESSENCIAL (achado r6) + dedup POR CURSO: o bench PARA o curso até alguém
            # corrigir a URL/o adaptador — num adaptador novo, um defeito PERMANENTE (a
            # sonda inconclusiva para sempre) ficava CALADO no log. Só o dono destrava,
            # então é essencial; a chave por curso deixa 1 ping por janela.
            # SEM FRASE DE SESSÃO MORTA: nem o texto nem o rótulo do curso podem casar as
            # âncoras de morte do classificador (o texto antigo tinha "sessão morta" e
            # "relogin" — ambos casam `_RE_SESSAO`). O curso passa pelo `rotulo_seguro`
            # (um slug/título da plataforma pode trazer um termo desses).
            alertas.captura_morreu(plat, _texto_bench(curso, n5), essencial=True,
                                   chave=("bench", curso))
            # E15: bench por exit-5 em série — irredutível POR-CURSO, anti-flap.
            _registrar(espinha, f"BENCH exit-5: travei {curso} após {n5} sondas "
                       f"inconclusivas seguidas",
                       "exit-5 idêntico em série (URL provável malformada) — sonda "
                       "inconclusiva, não é veredito sobre o acesso da conta: sem "
                       "reseed; avanço no Notion desbencha",
                       tipo="escalada", reversivel=True, escalada=True,
                       trava="bench-exit5", curso=curso, plataforma=plat,
                       fonte=fonte_causa, origem="athena-local/causa")
            return
    elif getattr(obito, "exit_code", None) is not None:
        st.pop("exit5_seguidas", None)                # morte CONFIRMADA de outra causa:
                                                      # a série 'idêntica' quebrou

    # transitório / TOKEN / desconhecida: back off (recozimento) e re-tenta sob o gate.
    # `escalar_token` cai AQUI (não mais irredutível): alertamos o dono para trocar a
    # chave, mas o disjuntor RECOZE e re-tenta — um blip de credencial se cura sozinho e
    # uma chave revogada re-tenta com backoff crescente + alerta, sem latch permanente
    # (a chave é uma API downstream, não a plataforma raspada: zero risco de ban).
    try:
        disjuntor.registrar_falha(st, agora)
    except Exception:
        pass
    if acao == "escalar_token":
        # ESSENCIAL (só o dono troca a chave) + dedup por curso: o backoff re-tenta
        # e re-morre na mesma chave ruim — 1 ping por janela basta.
        alertas.captura_morreu(
            plat, f"credencial de API inválida — troque o token (curso {curso})",
            essencial=True, chave=("token", curso))
        # E2: credencial de API ruim → backoff + alerta (reversível, NÃO latcha —
        # a chave é API downstream, não a plataforma: recoze e re-tenta).
        _registrar(espinha, f"backoff+alerta de token em {curso}",
                   motivo_causa or "credencial de API inválida", reversivel=True,
                   escalada=True, trava=None, curso=curso, plataforma=plat,
                   fonte=fonte_causa, origem="athena-local/causa")
    elif acao == "escalar_humano":
        # ESSENCIAL (chama o dono) + dedup por curso na janela: o mesmo curso
        # re-morrendo da mesma causa não metralha o Telegram.
        # Causa NOMEADA (assinatura determinística: exit-4 com os erros do tracker,
        # net::ERR_ABORTED na navegação ou na sonda/reseed de sessão...) vai com o
        # MOTIVO nomeado — prefixá-la de "causa desconhecida" mandava o dono procurar
        # uma incógnita que a autópsia JÁ tinha nomeado. O prefixo "causa desconhecida
        # (fail-closed)" fica SÓ quando ela é de fato desconhecida (fail-closed: sem LLM,
        # LLM falhou/fora do conjunto; ou o LLM que respondeu "escalar_humano").
        nomeada = (getattr(decisao, "fonte", None) == "deterministico"
                   and bool(motivo_causa))
        if nomeada:
            alertas.captura_morreu(plat, f"{motivo_causa} (curso {curso})",
                                   essencial=True, chave=("humano", curso))
            # E3': causa NOMEADA que exige humano → chama o dono com o motivo.
            _registrar(espinha, f"escalei {curso} (causa nomeada)", motivo_causa,
                       reversivel=True, escalada=True, trava=None,
                       curso=curso, plataforma=plat, fonte=fonte_causa,
                       origem="athena-local/causa")
        else:
            alertas.captura_morreu(
                plat, f"causa desconhecida (fail-closed): {motivo_causa}",
                essencial=True, chave=("humano", curso))
            # E3: causa desconhecida fail-closed → chama o dono.
            _registrar(espinha, f"escalei {curso} (causa desconhecida)",
                       motivo_causa or "fail-closed: causa desconhecida",
                       reversivel=True, escalada=True, trava="desconhecida",
                       curso=curso, plataforma=plat, fonte="fail-closed",
                       origem="athena-local/causa")
    else:
        # E4: morte transitória (relancar/aguardar_backoff/None) → recozer e
        # re-tentar sob o gate do disjuntor (decisão reversível, não escalada).
        _registrar(espinha, f"recozer e re-tentar {curso}",
                   motivo_causa or f"morte transitória (causa={acao})",
                   reversivel=True, curso=curso, plataforma=plat,
                   fonte=fonte_causa, origem="athena-local/causa")
    st["fase"] = FASE_NOVO


def _anotar_causa_na_autopsia(obito, decisao, plataforma):
    """OBSERVABILIDADE da autópsia: anota a `Decisao` classificada (acao/motivo/fonte)
    + plataforma NO JSON da autópsia já gravado pelo vigia (via `obito.autopsia_path`).
    Grava as chaves nos DOIS vocabulários — `acao`/`motivo` (da Decisao) e
    `causa`/`detalhe` (o que os leitores da autópsia buscam via .get()) — para a
    próxima morte nunca mais ler "causa desconhecida" quando a causa FOI classificada.
    Best-effort: um erro aqui JAMAIS derruba o ciclo (troca atômica tmp+replace,
    mesmo padrão do vigia; preserva todas as chaves existentes, ex.: stderr_tail)."""
    path = getattr(obito, "autopsia_path", "") or ""
    if not path:
        return
    try:
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
        acao = getattr(decisao, "acao", None)
        motivo = getattr(decisao, "motivo", "") or ""
        rec["acao"] = acao
        rec["causa"] = acao
        rec["motivo"] = motivo
        rec["detalhe"] = motivo
        rec["fonte"] = getattr(decisao, "fonte", "") or ""
        rec["plataforma"] = plataforma
        # morte SEM FALHA da captura (reinício do Mac / órfã que saiu limpa): o vigia
        # NÃO a conta no flap das próximas mortes desta conta.
        rec["sem_falha"] = bool(getattr(decisao, "sem_falha", False))
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def _autopsiar_ciclo(executor, estado, *, vigia, causa, disjuntor, alertas, lock_dir,
                     autopsia_dir, agora, meta_por_curso, flap_min, llm, espinha=None,
                     boot_ts=None):
    """Passe de AUTÓPSIA do ciclo: drena os óbitos do executor, roda o vigia (que também
    varre `lock_dir` por mortes de encarnações anteriores), classifica cada óbito pela
    causa-raiz e aplica a decisão ao `st` do curso. Best-effort: um erro aqui NÃO derruba
    o ciclo (a passada ainda tem o fallback de morte).

    Morte SÓ-DE-LOCK (captura de encarnação anterior; o `drenar_obitos` só conhece os
    filhos DESTA): o vigia recebe `stderr_path_de` = `executor._stderr_path` (o .err da
    conta, que o `_spawn_popen` trunca a cada disparo — por isso a autópsia roda ANTES
    da passada que re-dispara) e `boot_ts` (None => o vigia lê o kern.boottime)."""
    stderr_por_conta = {}
    drenar = getattr(executor, "drenar_obitos", None)
    if callable(drenar):
        try:
            stderr_por_conta = drenar() or {}
        except Exception:
            stderr_por_conta = {}
    extra = {}
    stderr_path_de = getattr(executor, "_stderr_path", None)
    if callable(stderr_path_de):
        extra["stderr_path_de"] = stderr_path_de
    if boot_ts is not None:
        extra["boot_ts"] = boot_ts
    try:
        obitos = vigia.autopsia(lock_dir, stderr_por_conta, agora=agora,
                                autopsia_dir=autopsia_dir, flap_min=flap_min, **extra)
    except Exception:
        obitos = []
    espinha = espinha or _EspinhaNula()
    for obito in obitos:
        curso = getattr(obito, "curso", "") or ""
        if not curso:
            continue
        st = estado.setdefault(curso, {})
        try:
            # `plataforma=` (item 4 da r15): o abort por excesso de falhas do motor diz
            # "Algo está sistematicamente errado do lado da Hotmart" em TODA plataforma.
            # Passando o rótulo do YAML, o motivo do exit-4 que vai ao alerta e ao JSON
            # da autópsia nomeia a plataforma do RUN (greenn/kiwify/alpaclass...).
            decisao = causa.classificar(
                obito, llm=llm, plataforma=_plataforma_de(curso, meta_por_curso))
        except Exception:
            decisao = None
        # FIX de observabilidade: a Decisao classificada vai PRO DISCO, no JSON da
        # autópsia desta morte — senão toda leitura posterior vê "causa desconhecida".
        if decisao is not None:
            _anotar_causa_na_autopsia(
                obito, decisao, _plataforma_de(curso, meta_por_curso))
        # E14: custo do diagnóstico `claude -p`. O seam só é consultado quando a
        # causa NÃO bateu numa assinatura determinística (fonte != "deterministico");
        # com `llm` ligado, isso significa que houve UMA chamada headless — custo
        # PRESUMIDO por proxy fixo, SEMPRE medido=False (regra inviolável nº2:
        # jamais somar presumido com medido).
        if llm is not None and getattr(decisao, "fonte", None) in ("llm", "fail-closed"):
            plat = _plataforma_de(curso, meta_por_curso)
            _registrar(espinha, f"diagnóstico claude -p da morte de {curso}",
                       "custo presumido (o seam claude -p não devolve usage)",
                       tipo="custo", reversivel=True, modelo="claude -p",
                       custo_usd=_CUSTO_PROXY_CLAUDE_P_USD, medido=False,
                       curso=curso, plataforma=plat,
                       fonte=getattr(decisao, "fonte", "llm"),
                       origem="athena-local/causa")
        _aplicar_decisao(curso, st, obito, decisao, disjuntor=disjuntor,
                         alertas=alertas, agora=agora, meta_por_curso=meta_por_curso,
                         flap_min=flap_min, espinha=espinha)


def _escalar_plataforma_nova(projeto_nome, voz, curso_url, st, *, espinha=None):
    """Gate da Capacidade B no doméstico: escala UMA vez (latch por-curso) que a
    plataforma é NOVA (sem adaptador) e PULA o curso. Não dispara criação de adaptador
    (decisão humana)."""
    if st.get("plataforma_nova_avisada"):
        return None
    plat = adaptador_pipeline.plataforma_de_url(curso_url)
    pedido = (f"[{projeto_nome}] {curso_url} está numa PLATAFORMA NOVA "
              f"('{plat or 'desconhecida'}', sem adaptador). NÃO capturo sem adaptador; "
              f"criar o adaptador precisa da SUA aprovação.")
    voz.escalar(Problema("plataforma_nova", curso_url, pedido, "aviso"), pedido)
    st["plataforma_nova_avisada"] = True
    # E6: plataforma sem adaptador — pulei (latch por-curso; reversível quando o
    # adaptador existir).
    _registrar(espinha or _EspinhaNula(), f"pulei {curso_url}: plataforma nova",
               f"sem adaptador para '{plat or 'desconhecida'}'", reversivel=True,
               escalada=True, trava="plataforma-nova", curso=curso_url,
               plataforma=plat or "desconhecida", origem="athena-local")
    return Acao("", False, True, pedido)


def _passada_local_fn(executor, progresso_fn, voz, estado, *, projeto_nome, disjuntor,
                      agora, cooldown_s=_COOLDOWN_SAIDA_LIMPA_S, espinha=None,
                      pendencia_fn=None, cooldown_sem_pendencia_s=_COOLDOWN_SEM_PENDENCIA_S,
                      adiados=None):
    """Fábrica da `passada_fn` LOCAL que o owner (`orquestrar_captura`) invoca por curso.

    A máquina de estados por-curso (persiste em `estado[curso]` entre ciclos):
      1. já CONCLUIDO -> None (idempotente).
      2. lê a contagem-verdade do Notion. Ilegível -> escala honesto, NÃO dispara às cegas.
      3. AVANÇO detectado (no_notion subiu) -> `disjuntor.registrar_sucesso` re-arma o
         recozimento (backoff volta ao degrau zero).
      4. COMPLETO no Notion (no_notion >= total, total>0) -> anti-dup por COMPLETUDE.
      5. processo AINDA ativo -> quieto (None): captura em andamento (anti-ban/disjuntor).
      5b. a CONTA tem óbito colhido e ainda não autopsiado (reap no meio da passada) ->
         quieto (None): a autópsia do próximo ciclo classifica e decide (sem dupla
         contagem, sem re-disparo antes da causa).
      6. FALLBACK de morte: estava CAPTURANDO, não está mais ativo, e a autópsia do ciclo
         NÃO tratou -> conta uma falha (backoff) e volta a NOVO (never-stop).
      7. `disjuntor.pode_tentar` FECHADO (teto/backoff/irredutível) -> escala UMA vez (latch).
      8. senão -> DISPARA. `ContaOcupada` (anti-ban) => aguarda a vez (None). Falha/silêncio
         do disparo -> escala honesto. `MaquinaSobrecarregada` (portão de carga) => ADIADO
         neste ciclo (None): NÃO é falha (sem disjuntor/flap/tentativa, sem alerta por
         curso); decisão "adiei <curso>: <motivo>" 1x por episódio e o curso entra em
         `adiados` (a lista do ciclo, p/ o aviso AGREGADO de `ciclo_local`).
    """
    esp = espinha or _EspinhaNula()

    def passada(curso):
        st = estado.setdefault(curso, {})
        plat = adaptador_pipeline.plataforma_de_url(curso) or curso
        if st.get("fase") == FASE_CONCLUIDO:
            return None
        try:
            no_notion, total = progresso_fn(curso)
        except Exception as e:
            pedido = (f"[{projeto_nome}] NÃO consegui ler o Notion p/ {curso} "
                      f"(anti-dup/completude): {str(e)[:140]} — NÃO disparo às cegas")
            voz.escalar(Problema("progresso_notion_inacessivel", curso, pedido, "aviso"),
                        pedido)
            # E12: progresso Notion ilegível → não disparo às cegas (fail-closed).
            # LATCH (regra de ruído "só transição"): registra na ENTRADA do estado
            # ilegível; um Notion persistentemente fora não spamma um log por ciclo.
            # O latch limpa numa leitura bem-sucedida (abaixo).
            if not st.get("notion_ilegivel_avisado"):
                _registrar(esp, f"não li o Notion de {curso} — não disparo",
                           f"{str(e)[:140]}", tipo="escalada", reversivel=True,
                           escalada=True, fonte="fail-closed", curso=curso,
                           plataforma=plat, origem="athena-local/passada")
                st["notion_ilegivel_avisado"] = True
            return Acao("", False, True, pedido)
        st["notion_ilegivel_avisado"] = False              # leitura OK: re-arma o latch E12
        # AVANÇO -> re-arma o recozimento do disjuntor (o backoff reduz com sucesso).
        if no_notion is not None:
            prev = st.get("ultimo_no_notion")
            if prev is not None and int(no_notion) > int(prev):
                try:
                    disjuntor.registrar_sucesso(st)
                except Exception:
                    pass
                st.pop("cooldown_ate", None)               # AVANÇO real: sai do cooldown (há trabalho)
                st.pop("exit5_seguidas", None)             # a captura PRODUZIU: sonda sã, zera o bench
                if st.pop("benched_exit5", None):          # DESBENCH (never-stop): avanço real
                    st.pop("irredutivel", None)            # ... reabre SÓ o bench de exit-5
                    st.pop("esgotado_avisado", None)       # (reseed nunca seta benched_exit5)
            st["ultimo_no_notion"] = int(no_notion)
        if no_notion is not None and int(total) > 0 and int(no_notion) >= int(total):
            st["fase"] = FASE_CONCLUIDO
            acao = Acao(f"[{projeto_nome}] {curso} COMPLETO no Notion "
                        f"({no_notion}/{total}) — não disparo (anti-dup por completude)",
                        True, False)
            voz.avisar_acao(acao)
            # E7: curso COMPLETO no Notion — decidi não disparar (anti-dup por
            # completude). Transição NOVO/CAPTURANDO→CONCLUIDO (uma vez).
            _registrar(esp, f"{curso} COMPLETO ({no_notion}/{total}) — não disparo",
                       "anti-dup por completude (fonte-verdade Notion)",
                       reversivel=True, fonte="deterministico", curso=curso,
                       plataforma=plat, origem="athena-local/passada")
            return acao
        if executor.curso_ativo(curso):
            return None                                    # capturando: quieto
        # MORTE AINDA NÃO AUTOPSIADA (incidente 10/09): o reap roda em QUALQUER consulta ao
        # executor — aqui mesmo, no curso_ativo acima, DEPOIS da autópsia deste ciclo e de
        # uma leitura do Notion de até 120s. A conta tem então um óbito que a causa ainda
        # não classificou; quem decide é a autópsia do PRÓXIMO ciclo (1 falha, a causa
        # certa: sessão morta trava, saída limpa arma cooldown). Agir agora era o FALLBACK
        # contar a falha E a autópsia contar de novo (dupla contagem), e re-disparar a conta
        # ANTES da causa (numa sessão morta: mais uma sonda na superfície de ban). Espera UM
        # ciclo — não é falha, não escala, não conta tentativa.
        if _aguardando_autopsia(executor, curso):
            return None
        # PENDÊNCIA CAPTURÁVEL (tracker), lida UMA vez por passada e ANTES da máquina de
        # cooldown: serve (a) de BASELINE no arme do cooldown de saída-limpa e (b) de
        # gatilho never-stop DENTRO da janela (pendência NOVA limpa o cooldown na hora —
        # BLOQUEANTE-2 do review: antes só avanço-no-Notion/morte limpavam, e um reseed
        # dentro da janela ficava preso). None = desconhecido (fail-open).
        pend = None
        if pendencia_fn is not None:
            try:
                pend = pendencia_fn(curso)
            except Exception:
                pend = None                                # fail-open: higiene nunca trava captura
        # O processo encerrou. Distinguir SAÍDA LIMPA (concluído/quiescido) de MORTE.
        if st.get("fase") == FASE_CAPTURANDO:
            if st.get("_saida_limpa_ciclo") == agora:
                # SAÍDA LIMPA (exit 0): NÃO é falha (backoff intacto), NÃO escala. Se o
                # Notion NÃO avançou desde o disparo, o run nada produziu -> curso quiescido:
                # entra em COOLDOWN (evita re-spawn a cada ciclo, que floodava o vigia com
                # saídas-limpas). Se avançou, houve progresso -> sem cooldown (deixa
                # continuar já no próximo ciclo). Nunca conta morte/flap.
                ref = st.get("_no_notion_no_disparo")
                avancou = (no_notion is not None and ref is not None
                           and int(no_notion) > int(ref))
                if not avancou:
                    st["cooldown_ate"] = agora + cooldown_s
                    # BASELINE do never-stop: só pendência ACIMA disto é 'nova' e limpa a
                    # janela — a preexistente (ex.: sem_legenda à espera do --audio) NÃO
                    # limpa, senão o cooldown anti-flood viraria letra morta.
                    st["_pend_no_cooldown"] = pend
                    # E8: saída limpa sem avanço → curso quiescido entra em cooldown.
                    _registrar(esp, f"{curso} quiescido — cooldown {int(cooldown_s)}s",
                               "saída limpa (exit 0) sem avanço no Notion",
                               reversivel=True, fonte="deterministico", curso=curso,
                               plataforma=plat, origem="athena-local/passada")
                st["fase"] = FASE_NOVO
            elif st.get("_morte_ciclo") != agora:
                # FALLBACK de MORTE (default/no-autópsia): caiu sem saída-limpa e a autópsia
                # NÃO tratou -> conta a falha (backoff) e reabilita a tentativa.
                try:
                    disjuntor.registrar_falha(st, agora)
                except Exception:
                    pass
                st["fase"] = FASE_NOVO
        # COOLDOWN de saída-limpa: curso quiescido fica quieto até a janela expirar (não é
        # falha nem escala — só evita o re-spawn apertado de um curso concluído). Ao expirar,
        # cai adiante e reavalia (never-stop). O cooldown limpa ANTES de expirar em: avanço
        # no Notion, morte real, e (aqui) pendência capturável NOVA acima do baseline do
        # arme — reseed/conteúdo novo dentro da janela re-seleciona NA HORA (never-stop).
        # Baseline desconhecido (None) => conservador: a janela segura até expirar.
        cd = st.get("cooldown_ate")
        if cd is not None and agora < cd:
            base = st.get("_pend_no_cooldown")
            if pend is not None and base is not None and int(pend) > int(base):
                st.pop("cooldown_ate", None)               # NEVER-STOP: há trabalho NOVO
                st.pop("sem_pendencia_avisado", None)      # re-arma o latch do E13
                # E14: pendência nova dentro da janela — cooldown limpo na hora.
                _registrar(esp, f"{curso}: pendência nova ({base}→{pend}) limpou o cooldown",
                           "never-stop: trabalho capturável apareceu dentro da janela "
                           "(reseed/conteúdo novo) — re-seleciono já",
                           reversivel=True, fonte="deterministico", curso=curso,
                           plataforma=plat, origem="athena-local/passada")
            else:
                return None
        # HIGIENE (2) — SEM PENDÊNCIA CAPTURÁVEL ("essencialmente pronto"): o tracker prova
        # que só restam terminais (no_notion/sem_conteudo/falhou…), mesmo com o Notion <
        # total. NÃO re-disparar NEM escalar exit-4 a cada ciclo (falso invistodireito
        # 533/547): entra em cooldown LONGO. Roda ANTES do disjuntor/dispatch, de modo que
        # o curso nem é RE-SELECIONADO (não spawna -> não morre -> não vira exit-4 escalado).
        # None (desconhecido/não semeado) => fail-open (segue o fluxo antigo). >0 (pendência
        # REAL: parede/throttle deixam a aula PENDENTE/in-flight) => NÃO cai aqui e SEGUE
        # para o disjuntor/escala — a INVARIANTE "done != bloqueado".
        if pend == 0:
            st["cooldown_ate"] = agora + cooldown_sem_pendencia_s
            st["_pend_no_cooldown"] = 0                     # baseline 'done': QUALQUER >0 é nova
            if not st.get("sem_pendencia_avisado"):
                st["sem_pendencia_avisado"] = True          # latch: só loga a TRANSIÇÃO
                _registrar(esp, f"{curso} sem pendência capturável — cooldown "
                           f"{int(cooldown_sem_pendencia_s)}s",
                           "só restam aulas terminais (sem_conteudo/falhou/no_notion) — "
                           "NÃO re-disparo nem escalo exit-4 (Notion < total é benigno)",
                           reversivel=True, fonte="deterministico", curso=curso,
                           plataforma=plat, origem="athena-local/passada")
            return None
        if pend is not None and pend > 0:
            st.pop("sem_pendencia_avisado", None)           # há trabalho: re-arma o latch
        try:
            pode = disjuntor.pode_tentar(st, agora)
        except Exception:
            pode = True
        if not pode:
            pedido = (f"[{projeto_nome}] {curso} NÃO concluiu e o processo não está mais "
                      f"ativo — DISJUNTOR ABERTO (teto/backoff/irredutível): paro de "
                      f"disparar e escalo (não martelo). Re-arma quando o backoff expira / "
                      f"há avanço; irredutível (reseed/token) só o humano destrava.")
            if not st.get("esgotado_avisado"):
                voz.escalar(Problema("captura_local_esgotada", curso, pedido, "critico"),
                            pedido)
                st["esgotado_avisado"] = True
                # E9: disjuntor fechado (teto/backoff/irredutível) — paro de disparar.
                _registrar(esp, f"paro de disparar {curso} (disjuntor aberto)",
                           "teto/backoff/irredutível — escalo, não martelo",
                           tipo="escalada", reversivel=True, escalada=True,
                           fonte="disjuntor", curso=curso, plataforma=plat,
                           origem="athena-local/passada")
            return Acao("", False, True, pedido)
        st["esgotado_avisado"] = False                     # disjuntor reabriu: re-arma o latch
        try:
            conf = executor.disparar(curso)
        except captura.MaquinaSobrecarregada as e:
            # PORTÃO DE CARGA (incidente 10/09): Mac sufocado => ADIA. Não é falha: nada de
            # registrar_falha, tentativa, fase CAPTURANDO nem escalada por curso. Decisão
            # registrada na ENTRADA do episódio (latch: uma sobrecarga de horas não vira uma
            # linha por ciclo por curso); o aviso AGREGADO do ciclo é do `ciclo_local`.
            # `DisparoEscalonado` (a RAMPA da saída da sobrecarga) cai aqui também: mesmo
            # tratamento; o 3º campo diz ao aviso agregado que NÃO é sobrecarga (a máquina
            # já aliviou — não prolonga o episódio nem o escala).
            motivo = str(getattr(e, "motivo", "") or e)
            st["_adiado_ciclo"] = agora
            if adiados is not None:
                adiados.append((curso, motivo, bool(getattr(e, "escalonamento", False))))
            if not st.get("adiado_carga_avisado"):
                st["adiado_carga_avisado"] = True
                _registrar(esp, f"adiei {curso}: {motivo}",
                           "portão de carga: disparo ADIADO neste ciclo — NÃO é falha (não "
                           "conta disjuntor, flap nem tentativa; sem alerta por curso); "
                           "reavalio a cada ciclo e disparo quando a máquina aliviar",
                           reversivel=True, fonte="guard", curso=curso, plataforma=plat,
                           origem="athena-local/portao-carga")
            return None
        except captura.ContaOcupada:
            st.pop("adiado_carga_avisado", None)           # não é mais adiamento por carga
            return None                                    # anti-ban: aguarda a vez (não é falha)
        except Exception as e:
            st.pop("adiado_carga_avisado", None)           # o portão deixou passar: re-arma
            pedido = (f"[{projeto_nome}] FALHEI ao disparar a captura LOCAL de {curso}: "
                      f"{str(e)[:160]}")
            voz.escalar(Problema("captura_local_disparo_falhou", curso, pedido, "critico"),
                        pedido)
            # E11: disparo falhou — escalo, não assumo sucesso.
            _registrar(esp, f"falhei ao disparar {curso}", f"{str(e)[:160]}",
                       tipo="escalada", reversivel=True, escalada=True,
                       fonte="fail-closed", curso=curso, plataforma=plat,
                       origem="athena-local/passada")
            return Acao("", False, True, pedido)
        st.pop("adiado_carga_avisado", None)               # o portão deixou passar: re-arma
        if not conf:
            pedido = (f"[{projeto_nome}] disparo LOCAL de {curso} SEM confirmação — "
                      f"não assumo sucesso")
            voz.escalar(Problema("captura_local_sem_confirmacao", curso, pedido, "critico"),
                        pedido)
            # E11: disparo sem confirmação — não assumo sucesso (regra nº1).
            _registrar(esp, f"disparo de {curso} sem confirmação", "não assumo sucesso",
                       tipo="escalada", reversivel=True, escalada=True,
                       fonte="fail-closed", curso=curso, plataforma=plat,
                       origem="athena-local/passada")
            return Acao("", False, True, pedido)
        st["tentativas"] = st.get("tentativas", 0) + 1
        st["fase"] = FASE_CAPTURANDO
        # Marca-d'água do Notion no disparo: se a saída-limpa não a ultrapassar, o run nada
        # produziu (curso quiescido -> cooldown); se ultrapassar, houve progresso (segue).
        st["_no_notion_no_disparo"] = st.get("ultimo_no_notion")
        acao = Acao(f"[{projeto_nome}] captura LOCAL de {curso} iniciada "
                    f"(tentativa {st['tentativas']}): {conf}", True, False)
        voz.avisar_acao(acao)
        # E10: disparei a captura (tentativa N) — decisão reversível.
        _registrar(esp, f"disparei captura de {curso} (tentativa {st['tentativas']})",
                   f"{conf}", reversivel=True, fonte="deterministico", curso=curso,
                   plataforma=plat, origem="athena-local/passada")
        return acao
    return passada


# ===========================================================================
# SUPERVISÃO DE SISTEMAS GERADOS (F4-d) — um sistema é um ALVO ao lado da captura.
# Integração por ARQUIVO: lê heartbeat/resultado.json do <raiz>/estado, NUNCA
# importa `sintetizador.*`. MESMO disjuntor/vigia/never-stop da captura.
# ===========================================================================
# Frescor do heartbeat: acima disso, com run "ativo", o run está TRAVADO (processo
# vivo mas parado) — matar é seguro (sistema não tem sessão/anti-ban). Default 15min.
_HEARTBEAT_LIMIAR_S = float(os.getenv("ATHENA_SISTEMA_HEARTBEAT_S", "900"))
# Proxy conservador de custo quando o resultado.json não traz custo (nunca deve,
# mas fail-safe): 0 medido, 0 presumido — o resultado.json é a fonte-verdade.


def _ler_json_tolerante(path):
    """Lê um JSON de sistema à prova de falhas (nunca levanta; ausente/meio-escrito
    → None). O produtor (ledger/resultado) escreve atômico; isto só protege o
    leitor de uma corrida rara ou arquivo ausente."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _resultado_mais_recente(raiz):
    """O `estado/resultado-*.json` mais novo (por mtime) do sistema, ou None. É o
    desfecho do último run; a Athena o lê por ciclo (§3.3)."""
    import glob
    d = os.path.join(str(raiz), "estado")
    cands = glob.glob(os.path.join(d, "resultado-*.json"))
    if not cands:
        return None
    cands.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return _ler_json_tolerante(cands[0])


def _build_mais_recente(raiz):
    """O `estado/build-*.json` mais novo (por mtime), ou None. Desfecho de uma
    RECONSTRUÇÃO (F4-e); a Athena o observa por arquivo — nunca por óbito."""
    import glob
    d = os.path.join(str(raiz), "estado")
    cands = glob.glob(os.path.join(d, "build-*.json"))
    if not cands:
        return None
    cands.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return _ler_json_tolerante(cands[0])


# ---------------------------------------------------------------------------
# F4-e/F4-f — gatilhos de correção + prestação de contas por sistema.
# ---------------------------------------------------------------------------
# Modo do gate D5 (§F.2): 'alerta' (default, ordem do usuário 22/07 — registra +
# alerta e DISPARA) | 'pausa' (não dispara). O flip para 'pausa' é decisão do usuário.
_BUDGET_MODO = os.getenv("ATHENA_BUDGET_MODO", "alerta")
# Proxy de custo do brainstorm `claude -p` antes de escalar — SEMPRE medido=False
# (regra inviolável nº2: jamais somar presumido com medido).
_CUSTO_PROXY_BRAINSTORM_USD = float(os.getenv("ATHENA_CUSTO_PROXY_BRAINSTORM_USD", "0.05"))

# Classes de trava do ledger que são DEFEITO DE ENGENHARIA → reconstruir (§E.1).
_TRAVAS_ENGENHARIA = frozenset({
    "executor_ausente", "desconhecida", "reprovacao_esgotada",
    "erro_nao_classificado", "indeterminado_esgotado", "timeout_repetido"})
# NÃO-defeito: calibração D2 pendente — alerta 1×, NENHUMA reescrita.
_TRAVAS_NAO_DEFEITO = frozenset({"irreversivel-externo"})
# Budget: não-engenharia (re-run só sob disjuntor+D5; reincidente → retro-síntese F6).
_TRAVAS_BUDGET = frozenset({"budget", "custo_teto"})


def _dia_local_ts(agora) -> str:
    """Dia LOCAL do epoch `agora` (float) — chave do disjuntor de engenharia e do
    latch do gate D5. Ilegível → hoje (fail-safe, nunca levanta)."""
    from datetime import datetime as _dt
    try:
        return _dt.fromtimestamp(float(agora)).date().isoformat()
    except Exception:
        return _dt.now().date().isoformat()


def _ler_ledger_tolerante(raiz, run_id):
    """Lê `estado/runs/<run_id>.jsonl` do lado ATHENA, tolerante (linha inválida
    pulada) e SEM importar `sintetizador.*` — integração por ARQUIVO (§E.1). Lista
    de eventos (dicts) na ordem do arquivo; ausente/ilegível → []."""
    if not run_id:
        return []
    path = os.path.join(str(raiz), "estado", "runs", f"{run_id}.jsonl")
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for linha in f:
                linha = linha.strip()
                if not linha:
                    continue
                try:
                    reg = json.loads(linha)
                except Exception:
                    continue
                if isinstance(reg, dict):
                    out.append(reg)
    except (FileNotFoundError, OSError):
        return []
    return out


def _incidente_da_cauda(raiz, run_id):
    """Classifica um run congelado pela CAUDA do ledger (§E.1.2): acha o ÚLTIMO
    `g4_escalada`/`g2_congelada` e devolve `{trava, etapa, classe}` com
    classe ∈ {'nao-defeito','budget','engenharia'}. Sem cauda relevante → None.

    É o que corrige o BUG do rótulo fixo: um congelamento por `executor_ausente`
    (engenharia) NÃO pode virar `irreversivel-externo` (calibração), senão a
    correção nunca dispara. A trava REAL vem daqui, não de um literal."""
    eventos = _ler_ledger_tolerante(raiz, run_id)
    alvo = None
    for reg in eventos:
        if reg.get("evento") in ("g4_escalada", "g2_congelada"):
            alvo = reg                                 # o ÚLTIMO vence (cauda)
    if alvo is None:
        return None
    trava = alvo.get("trava")
    etapa = alvo.get("etapa")
    if trava in _TRAVAS_NAO_DEFEITO:
        classe = "nao-defeito"
    elif trava in _TRAVAS_BUDGET:
        classe = "budget"
    else:
        # executor_ausente / desconhecida / reprovacao_esgotada / None → engenharia.
        classe = "engenharia"
    return {"trava": trava, "etapa": etapa, "classe": classe}


def _gate_d5(slug, spec, st, espinha, agora, *, gasto_por_sistema_fn, budget_modo,
             alertas):
    """Gate D5 (§F.2), ANTES de TODO disparo (run E build). Compara a SOMA
    conservadora (medido+presumido — SÓ para o teto, nunca para relato) com o teto
    do sistema. Modo 'alerta' (default): registra (latch 1×/dia/sistema) + alerta e
    DISPARA; 'pausa': NÃO dispara. Devolve True se pode disparar.

    Sem `gasto_por_sistema_fn` (default null-object) o gate é no-op (comportamento
    pré-integração preservado)."""
    if gasto_por_sistema_fn is None:
        return True
    try:
        g = gasto_por_sistema_fn().get(slug) or {}
    except Exception:
        return True                                    # observabilidade nunca bloqueia
    conservador = (float(g.get("custo_medido_usd", 0.0) or 0.0)
                   + float(g.get("custo_presumido_usd", 0.0) or 0.0))
    teto = float(getattr(spec, "teto_dia_usd", 5.0) or 5.0)
    if conservador < teto:
        return True
    dia = _dia_local_ts(agora)
    if st.get("d5_avisado_dia") != dia:                # latch 1×/dia/sistema
        st["d5_avisado_dia"] = dia
        try:
            alertas.captura_morreu(
                slug, f"sistema {slug}: teto D5 US${teto:.2f}/dia atingido "
                f"(conservador US${conservador:.4f}) — modo {budget_modo}",
                essencial=True)   # ordem 22/07: budget ALERTA (latch 1×/dia já dedupa)
        except Exception:
            pass
        _registrar(espinha, f"teto D5 atingido em {slug} (modo {budget_modo})",
                   f"soma conservadora medido+presumido US${conservador:.4f} "
                   f">= teto US${teto:.2f}", tipo="escalada", reversivel=True,
                   escalada=True, trava="budget", sistema=slug,
                   fonte="deterministico", origem="athena-local/sistema")
    return budget_modo != "pausa"


def _brainstorm_gate_escalar(slug, etapa, st, *, alertas, espinha, llm, motivo):
    """Antes de ESCALAR ao humano ([[trava-brainstorm-fable]]): com LLM, 1 linha de
    brainstorm (custo medido=False, proxy) e depois escala 'só humano destrava';
    sem LLM, escala direto (fail-closed). Nunca constrói (é o fim da linha da
    engenharia)."""
    if llm is not None:
        _registrar(espinha, f"brainstorm da falha de {slug}:{etapa}",
                   "custo presumido (o seam claude -p não devolve usage)",
                   tipo="custo", reversivel=True, modelo="claude -p",
                   custo_usd=_CUSTO_PROXY_BRAINSTORM_USD, medido=False,
                   sistema=slug, fonte="llm", origem="athena-local/brainstorm")
    try:
        alertas.captura_morreu(slug, f"sistema {slug}: {motivo} — só humano destrava",
                               essencial=True, chave=("humano-sistema", slug))
    except Exception:
        pass
    _registrar(espinha, f"escalei {slug}:{etapa} — só humano destrava", motivo,
               tipo="escalada", reversivel=True, escalada=True, trava="desconhecida",
               sistema=slug, fonte="fail-closed", origem="athena-local/sistema")


def _observar_build(slug, spec, st, *, espinha, alertas, agora, llm):
    """Observa o desfecho de uma reconstrução (build-<id>.json novo, §E.4). pronto
    → 2 linhas de custo (origem='athena/reescrita', medido/presumido SEPARADOS) +
    `retentar_devido` (re-run sob gate) + marca `aguardando_rerun`. escalar/crash →
    brainstorm-gate → escalar_humano. Idempotente por build_id."""
    build = _build_mais_recente(spec.raiz)
    if build is None:
        return
    bid = build.get("build_id")
    if not bid or bid == st.get("ultimo_build"):
        return
    etapa = build.get("etapa")
    eng = st.setdefault("eng", {})
    reg_e = eng.get(etapa) or {}
    if not reg_e.get("aguardando_build"):
        return                                         # build não-esperado: ignora
    st["ultimo_build"] = bid
    reg_e["aguardando_build"] = False
    medido = float(build.get("custo_medido_usd", 0.0) or 0.0)
    presumido = float(build.get("custo_presumido_usd", 0.0) or 0.0)
    # CUSTO do build: 2 linhas SEPARADAS, no orçamento do SLUG (engenharia gasta o
    # teto do sistema). origem='athena/reescrita' as diferencia no SITREP.
    _registrar(espinha, f"custo medido da reescrita de {slug}:{etapa}",
               "usage real do build", tipo="custo", reversivel=True,
               custo_usd=medido, medido=True, sistema=slug, origem="athena/reescrita")
    _registrar(espinha, f"custo presumido da reescrita de {slug}:{etapa}",
               "estimativa (proxy)", tipo="custo", reversivel=True,
               custo_usd=presumido, medido=False, sistema=slug, origem="athena/reescrita")
    if bool(build.get("pronto")) is True:
        reg_e["aguardando_rerun"] = True
        st["retentar_devido"] = True                   # re-run SOB o gate (§E.4)
        _registrar(espinha, f"reescrita de {slug}:{etapa} PRONTA (hash novo)",
                   "executor reconstruído e provado (smoke+gate F2) — re-run sob gate",
                   reversivel=True, sistema=slug, fonte="deterministico",
                   origem="athena/reescrita")
    else:
        _brainstorm_gate_escalar(
            slug, etapa, st, alertas=alertas, espinha=espinha, llm=llm,
            motivo=f"reescrita não instalou (build escalou/crashou): {build.get('motivo','')[:120]}")
        reg_e["aguardando_rerun"] = False
    eng[etapa] = reg_e


def _passo_engenharia(slug, spec, st, executor, *, alertas, espinha, agora,
                      gasto_por_sistema_fn, budget_modo, llm):
    """Consome `st['eng_pendente']` (um gatilho de engenharia aceito): DISJUNTOR DE
    ENGENHARIA (máx. 1 ciclo/etapa/dia), GATE D5, então `disparar_build` (§E.4).
    Devolve Acao|None. Nunca há build+run simultâneos (mesmo lock do slug)."""
    pend = st.get("eng_pendente")
    if not pend:
        return None
    etapa = pend.get("etapa")
    incidente = pend.get("incidente")
    classe = pend.get("classe") or "executor_ausente"
    if not etapa:
        # Sem etapa reconstruível (ex.: exit 30 PlanoInvalido) → re-síntese é F6.
        st.pop("eng_pendente", None)
        _brainstorm_gate_escalar(slug, "?", st, alertas=alertas, espinha=espinha,
                                 llm=llm, motivo="defeito sem etapa reconstruível (re-síntese é F6)")
        return Acao(f"[{slug}] engenharia sem etapa — escalado", True, False)
    if not hasattr(executor, "disparar_build"):
        return None                                    # executor sem porta de build (mantém pendente)
    dia = _dia_local_ts(agora)
    eng = st.setdefault("eng", {})
    reg_e = eng.get(etapa) or {}
    # DISJUNTOR DE ENGENHARIA: já construiu esta etapa HOJE → 2º gatilho não constrói.
    if reg_e.get("dia") == dia and reg_e.get("builds", 0) >= 1:
        st.pop("eng_pendente", None)
        _registrar(espinha, f"2º gatilho de engenharia em {etapa} hoje — não construo",
                   f"disjuntor de engenharia (1 ciclo/etapa/dia local); incidente {incidente}",
                   tipo="escalada", reversivel=True, escalada=True, trava=classe,
                   sistema=slug, fonte="disjuntor", origem="athena-local/sistema")
        _brainstorm_gate_escalar(slug, etapa, st, alertas=alertas, espinha=espinha,
                                 llm=llm, motivo="2º gatilho de engenharia no mesmo dia")
        return Acao(f"[{slug}] disjuntor de engenharia: 2º gatilho em {etapa} hoje", True, False)
    # GATE D5 (antes do build — build consome o mesmo orçamento do sistema). Em
    # modo PAUSA o incidente NÃO é descartado: fica pendente e re-tenta quando o
    # teto reabrir (never-stop) — por isso NÃO se dá pop aqui.
    if not _gate_d5(slug, spec, st, espinha, agora,
                    gasto_por_sistema_fn=gasto_por_sistema_fn, budget_modo=budget_modo,
                    alertas=alertas):
        return None                                    # modo pausa: mantém eng_pendente
    st.pop("eng_pendente", None)                        # daqui em diante o gatilho é consumido
    feedback_path = None
    try:
        feedback_path = executor.escrever_feedback(slug, {
            "trava": classe, "run_id": incidente, "etapa": etapa, "classe": classe,
            "stderr_tail": pend.get("stderr_tail", ""), "laudo": pend.get("laudo", "")})
    except Exception:
        feedback_path = None
    try:
        conf = executor.disparar_build(slug, etapa, feedback_path)
    except Exception as e:
        _registrar(espinha, f"falhei ao disparar reescrita de {slug}:{etapa}",
                   f"{str(e)[:160]}", tipo="escalada", reversivel=True, escalada=True,
                   fonte="fail-closed", sistema=slug, origem="athena-local/sistema")
        return Acao("", False, True, f"[{slug}] disparo de build falhou: {str(e)[:160]}")
    eng[etapa] = {"dia": dia, "incidente": incidente,
                  "builds": reg_e.get("builds", 0) + 1, "aguardando_build": True,
                  "aguardando_rerun": False, "classe": classe}
    _registrar(espinha, f"aciono engenharia p/ {slug}:{etapa}",
               f"defeito classe={classe} no incidente {incidente} — reconstruo (E1→E6)",
               tipo="escalada", reversivel=True, escalada=True, trava=classe,
               sistema=slug, fonte="deterministico", origem="athena-local/reescrita")
    return Acao(f"[{slug}] reescrita de {etapa} disparada: {conf}", True, False)


def _aplicar_decisao_sistema(slug, st, obito, decisao, *, disjuntor, alertas, agora,
                             espinha, raiz=None):
    """Traduz a `Decisao` de `causa_sistema` (conjunto FECHADO, SEM reseed) em ação
    de never-stop sobre o `st` do sistema. Espelha `_aplicar_decisao` da captura,
    mas: matar já foi feito por quem drenou o óbito; aqui só se decide backoff /
    engenharia / escala. NUNCA há `escalar_reseed` (sistema não tem sessão)."""
    acao = getattr(decisao, "acao", None)
    fonte = getattr(decisao, "fonte", None) or "deterministico"
    motivo = getattr(decisao, "motivo", "") or ""
    st["ultima_causa"] = acao
    if acao == "nada":
        return
    # transitório (relancar/aguardar_backoff) e engenharia: avança o backoff do
    # disjuntor (recozimento) — o re-disparo virá sob o gate, nunca em martelo.
    try:
        disjuntor.registrar_falha(st, agora)
    except Exception:
        pass
    if acao == "acionar_engenharia":
        # SÓ-LOG por default (não-essencial): a engenharia RECONSTRÓI sozinha
        # (F4-e); se ela esgotar, o brainstorm-gate escala essencial ao humano.
        alertas.captura_morreu(slug, f"sistema {slug}: defeito → engenharia ({motivo})")
        # T1 (óbito exit 30/40/traceback): tenta identificar a etapa em curso pelo
        # heartbeat (run_id + etapa). Com etapa → FLAG de engenharia (o
        # `_passo_engenharia` reconstrói sob disjuntor+D5). Sem etapa (ex.: exit 30
        # PlanoInvalido antes de qualquer etapa) → só alerta/escalada (re-síntese é F6).
        etapa, run_id = None, None
        if raiz is not None:
            hb = _ler_json_tolerante(os.path.join(str(raiz), "estado", "heartbeat.json"))
            if isinstance(hb, dict):
                etapa, run_id = hb.get("etapa"), hb.get("run_id")
        if etapa:
            st["eng_pendente"] = {"etapa": etapa, "incidente": run_id or "obito",
                                  "classe": "executor_ausente",
                                  "stderr_tail": (getattr(obito, "stderr_tail", "") or "")[:400]}
        _registrar(espinha, f"aciono engenharia p/ {slug}"
                   + (f":{etapa}" if etapa else ""), motivo, tipo="escalada",
                   reversivel=True, escalada=True, trava="executor_ausente",
                   sistema=slug, fonte=fonte, origem="athena-local/causa-sistema")
    elif acao == "escalar_token":
        alertas.captura_morreu(slug, f"sistema {slug}: credencial de API — troque o token",
                               essencial=True, chave=("token-sistema", slug))
        _registrar(espinha, f"backoff+alerta de token em {slug}", motivo,
                   reversivel=True, escalada=True, sistema=slug, fonte=fonte,
                   origem="athena-local/causa-sistema")
    elif acao in ("escalar_humano", "pausar_sistema"):
        alertas.captura_morreu(slug, f"sistema {slug}: {acao} ({motivo})",
                               essencial=True, chave=(acao, slug))
        if acao == "pausar_sistema":
            st["pausado_por_causa"] = True
        _registrar(espinha, f"escalei {slug} ({acao})", motivo, tipo="escalada",
                   reversivel=True, escalada=True, trava="desconhecida",
                   sistema=slug, fonte="fail-closed",
                   origem="athena-local/causa-sistema")
    else:  # relancar / aguardar_backoff
        # NEVER-STOP: uma morte TRANSITÓRIA (recurso/rede) re-tenta SOB O GATE do
        # disjuntor (recozimento) — marca `retentar_devido` para o passo (4) re-
        # disparar quando o backoff expirar. Engenharia/token/humano/pausa NÃO
        # auto-re-tentam (precisam de correção/humano) — não marcam a flag.
        st["retentar_devido"] = True
        _registrar(espinha, f"recozer e re-tentar {slug}", motivo or f"transitório ({acao})",
                   reversivel=True, sistema=slug, fonte=fonte,
                   origem="athena-local/causa-sistema")


def _registrar_desfecho_sistema(slug, resultado, *, raiz, disjuntor, st, espinha,
                                alertas, agora, llm=None):
    """Um run TERMINADO (resultado.json novo) → prestação de contas por sistema:
    2 linhas de CUSTO (medido e presumido SEPARADOS, `sistema=slug`) + 1 linha de
    desfecho. Sucesso re-arma o disjuntor E FECHA um incidente de reescrita aberto;
    um congelamento é classificado pela TRAVA REAL da cauda do ledger (§E.1) —
    engenharia dispara a correção; calibração D2/budget não."""
    estado = resultado.get("estado")
    run_id = resultado.get("run_id")
    sucesso = bool(resultado.get("sucesso"))
    medido = float(resultado.get("custo_medido_usd", 0.0) or 0.0)
    presumido = float(resultado.get("custo_presumido_usd", 0.0) or 0.0)
    # CUSTO: duas linhas, JAMAIS somadas (regra inviolável nº2).
    _registrar(espinha, f"custo medido do run de {slug}", "usage real do run",
               tipo="custo", reversivel=True, custo_usd=medido, medido=True,
               sistema=slug, origem="athena-local/sistema")
    _registrar(espinha, f"custo presumido do run de {slug}", "estimativa (proxy)",
               tipo="custo", reversivel=True, custo_usd=presumido, medido=False,
               sistema=slug, origem="athena-local/sistema")
    if sucesso:
        try:
            disjuntor.registrar_sucesso(st)
        except Exception:
            pass
        _registrar(espinha, f"run de {slug} APROVADO (provado)", "sucesso falha-fechada",
                   reversivel=True, sistema=slug, fonte="deterministico",
                   origem="athena-local/sistema")
        # FECHA um incidente de reescrita aberto: re-run aprovado após reconstrução
        # (§E.4). reversivel=True — manifesto/hash anteriores permitem reinstalar.
        for etapa, reg_e in list((st.get("eng") or {}).items()):
            if reg_e.get("aguardando_rerun"):
                reg_e["aguardando_rerun"] = False
                _registrar(espinha,
                           f"reescrita de {etapa} fechou incidente {reg_e.get('incidente')}",
                           "re-run aprovado após reconstrução (hash novo instalado)",
                           reversivel=True, sistema=slug, fonte="deterministico",
                           origem="athena/reescrita")
        return
    # Não-sucesso: a TRAVA REAL vem da cauda do ledger (corrige o rótulo fixo).
    eh_congelado = estado == "congelado_parcial"
    inc = _incidente_da_cauda(raiz, run_id) if eh_congelado else None
    trava_real = inc.get("trava") if inc else None
    classe = inc.get("classe") if inc else None
    etapa = inc.get("etapa") if inc else None
    _registrar(espinha, f"run de {slug} → {estado}",
               f"congelado_parcial/parcial (trava={trava_real})",
               tipo="escalada" if eh_congelado else "decisao",
               reversivel=True, escalada=eh_congelado,
               trava=trava_real if eh_congelado else None,
               sistema=slug, fonte="deterministico", origem="athena-local/sistema")
    if not (eh_congelado and classe == "engenharia" and etapa):
        return                                         # D2/budget/parcial: sem reescrita
    reg_e = (st.get("eng") or {}).get(etapa) or {}
    # 2ª FALHA do MESMO incidente (re-run após build congelou de novo, mesma etapa)
    # → escalar_humano direto, NUNCA 3º build (§E.2).
    if reg_e.get("aguardando_rerun") and reg_e.get("incidente") != run_id:
        reg_e["aguardando_rerun"] = False
        st.setdefault("eng", {})[etapa] = reg_e
        _brainstorm_gate_escalar(slug, etapa, st, alertas=alertas, espinha=espinha,
                                 llm=llm, motivo="2ª falha do mesmo incidente após reescrita")
        return
    # Gatilho de engenharia aceito → FLAG (o `_passo_engenharia` aplica disjuntor+D5).
    st["eng_pendente"] = {"etapa": etapa, "incidente": run_id,
                          "classe": trava_real or "executor_ausente"}


def passada_sistema(spec, st, executor, *, vigia, causa_sistema, disjuntor, alertas,
                    espinha, agora, lock_dir, autopsia_dir, flap_min, llm=None,
                    heartbeat_limiar_s=_HEARTBEAT_LIMIAR_S,
                    gasto_por_sistema_fn=None, budget_modo=None):
    """A máquina de estados por-sistema (§4.2 + F4-e/F4-f), um passo por ciclo.
    Devolve uma `Acao` (reportável) ou None (quieto). NUNCA levanta (best-effort):
    um erro aqui não pode derrubar o loop nem afetar a captura ao lado.

    Ordem: (0) BUILD em andamento → quieto (nunca mata/dispara — mesmo lock do
    slug); (1) run ATIVO com heartbeat velho → TRAVADO: mata (seguro) + espinha;
    (2) óbitos → autópsia → causa_sistema → decisão (pode flagar engenharia T1);
    (2.5) observa build-<id>.json (reescrita concluída → custo + re-run/escala);
    (3) resultado.json novo → custo/desfecho + trava REAL da cauda (flag T2/T3);
    (3.5) ENGENHARIA: disjuntor de engenharia + gate D5 + disparar_build;
    (4) gatilho de disparo (run) sob gate do disjuntor + gate D5; (5) quieto."""
    slug = spec.slug
    espinha = espinha or _EspinhaNula()
    if budget_modo is None:
        budget_modo = _BUDGET_MODO

    # (0) BUILD (reconstrução F4-e) em andamento p/ este slug → quieto. Nunca é
    # morto (≠ run travado) nem concorre com um run (mesmo lock do slug).
    if hasattr(executor, "build_ativo"):
        try:
            if executor.build_ativo(slug):
                return None
        except Exception:
            pass

    # (1) run ATIVO: heartbeat fresco → quieto; velho → TRAVADO, mata (SEGURO).
    if executor.sistema_ativo(slug):
        hb = _ler_json_tolerante(os.path.join(str(spec.raiz), "estado", "heartbeat.json"))
        ts = (hb or {}).get("ts")
        fresco = False
        if isinstance(ts, (int, float)):
            fresco = (agora - ts) < heartbeat_limiar_s
        else:
            # heartbeat ISO string: parse tolerante; ilegível → NÃO mata (conservador).
            try:
                from datetime import datetime as _dt
                fresco = (agora - _dt.fromisoformat(ts).timestamp()) < heartbeat_limiar_s
            except Exception:
                fresco = True
        if fresco:
            return None
        # TRAVADO: matar é SEGURO (sistema não tem sessão/anti-ban — ≠ captura).
        try:
            executor.matar(slug)
        except Exception:
            pass
        _registrar(espinha, f"matei run travado de {slug} (heartbeat velho)",
                   "processo vivo mas parado — kill seguro (sem sessão/anti-ban)",
                   tipo="escalada", reversivel=True, escalada=True, sistema=slug,
                   fonte="deterministico", origem="athena-local/sistema")
        return Acao(f"[{slug}] run travado morto (heartbeat velho)", True, False)

    # (2) ÓBITOS drenados → autópsia → causa_sistema → decisão (backoff/engenharia/escala).
    try:
        obitos_fonte = executor.drenar_obitos() or {}
    except Exception:
        obitos_fonte = {}
    try:
        obitos = vigia.autopsia(lock_dir, obitos_fonte, agora=agora,
                                autopsia_dir=autopsia_dir, flap_min=flap_min)
    except Exception:
        obitos = []
    for obito in obitos:
        if getattr(obito, "saida_limpa", False):
            continue                                   # exit 0 limpo: desfecho, não morte
        try:
            decisao = causa_sistema.classificar(obito, llm=llm)
        except Exception:
            decisao = None
        if llm is not None and getattr(decisao, "fonte", None) in ("llm", "fail-closed"):
            _registrar(espinha, f"diagnóstico claude -p da morte de {slug}",
                       "custo presumido (o seam claude -p não devolve usage)",
                       tipo="custo", reversivel=True, modelo="claude -p",
                       custo_usd=_CUSTO_PROXY_CLAUDE_P_USD, medido=False,
                       sistema=slug, fonte=getattr(decisao, "fonte", "llm"),
                       origem="athena-local/causa-sistema")
        _aplicar_decisao_sistema(slug, st, obito, decisao, disjuntor=disjuntor,
                                 alertas=alertas, agora=agora, espinha=espinha,
                                 raiz=spec.raiz)

    # (2.5) BUILD concluído (reescrita F4-e): observa build-<id>.json → custo do
    # build + re-run (pronto) ou brainstorm-gate/escala (escalou/crashou).
    _observar_build(slug, spec, st, espinha=espinha, alertas=alertas, agora=agora,
                    llm=llm)

    # (3) resultado.json novo (run_fim visto) → custo/desfecho por sistema (uma vez).
    resultado = _resultado_mais_recente(spec.raiz)
    if resultado is not None:
        rid = resultado.get("run_id")
        if rid and rid != st.get("ultimo_resultado_run"):
            st["ultimo_resultado_run"] = rid
            _registrar_desfecho_sistema(slug, resultado, raiz=spec.raiz,
                                        disjuntor=disjuntor, st=st, espinha=espinha,
                                        alertas=alertas, agora=agora, llm=llm)

    # (3.5) ENGENHARIA (F4-e): um gatilho aceito (T1/T2/T3) reconstrói a etapa sob
    # o disjuntor de engenharia (1 ciclo/etapa/dia) + gate D5 — a ÚNICA porta de
    # mudança de código. Pausado (P5) nunca reconstrói.
    if not (spec.estado == "pausado" or st.get("pausado_por_causa")):
        acao_eng = _passo_engenharia(
            slug, spec, st, executor, alertas=alertas, espinha=espinha, agora=agora,
            gasto_por_sistema_fn=gasto_por_sistema_fn, budget_modo=budget_modo, llm=llm)
        if acao_eng is not None:
            return acao_eng

    # (4) GATILHO de disparo (sob_demanda): só dispara sob PEDIDO explícito
    # (`st['pedido_run']`) ou re-tentativa devida — NUNCA em loop de gasto. Sempre
    # sob o gate do disjuntor (recozimento). Pausado (controle P5) nunca re-dispara.
    if spec.estado == "pausado" or st.get("pausado_por_causa"):
        return None
    pediu = bool(st.pop("pedido_run", False))
    quer_disparar = pediu or st.get("retentar_devido")
    if not quer_disparar:
        return None
    try:
        pode = disjuntor.pode_tentar(st, agora)
    except Exception:
        pode = True
    if not pode:
        if not st.get("esgotado_avisado"):
            st["esgotado_avisado"] = True
            _registrar(espinha, f"paro de disparar {slug} (disjuntor aberto)",
                       "teto/backoff — escalo, não martelo", tipo="escalada",
                       reversivel=True, escalada=True, fonte="disjuntor",
                       sistema=slug, origem="athena-local/sistema")
        return None
    # GATE D5 (§F.2) ANTES do disparo do run: modo 'pausa' → não dispara; 'alerta'
    # (default) → registra+alerta e dispara. Consome o retentar_devido só quando
    # de fato dispara; em PAUSA, um pedido explícito é PRESERVADO (never-stop —
    # re-tenta quando o teto reabrir), nunca silenciosamente descartado.
    if not _gate_d5(slug, spec, st, espinha, agora,
                    gasto_por_sistema_fn=gasto_por_sistema_fn, budget_modo=budget_modo,
                    alertas=alertas):
        if pediu:
            st["pedido_run"] = True
        return None
    st["esgotado_avisado"] = False
    st["retentar_devido"] = False
    try:
        conf = executor.disparar(slug)
    except Exception as e:
        _registrar(espinha, f"falhei ao disparar sistema {slug}", f"{str(e)[:160]}",
                   tipo="escalada", reversivel=True, escalada=True, fonte="fail-closed",
                   sistema=slug, origem="athena-local/sistema")
        return Acao("", False, True, f"[{slug}] disparo falhou: {str(e)[:160]}")
    st["tentativas"] = st.get("tentativas", 0) + 1
    _registrar(espinha, f"disparei run de {slug} (tentativa {st['tentativas']})",
               f"{conf}", reversivel=True, sistema=slug, fonte="deterministico",
               origem="athena-local/sistema")
    return Acao(f"[{slug}] run disparado: {conf}", True, False)


def _escalonado(item) -> bool:
    """Um item de `adiados` veio da RAMPA da saída da sobrecarga (não é sobrecarga)?
    Aceita o formato antigo (curso, motivo) — sem o 3º campo = sobrecarga."""
    return len(item) > 2 and bool(item[2])


def _carregar_episodio_carga(path, *, agora, lacuna_s=None) -> dict:
    """Episódio de sobrecarga PERSISTIDO por uma encarnação anterior do loop, ou {}.

    Achado r6: o `desde` só em memória fazia um reinício (vigia externo, launchd) zerar o
    relógio — com reinícios mais curtos que ATHENA_CARGA_ALERTA_S o aviso ESSENCIAL nunca
    saía ("0 aulas" sem alerta). Descarta (e apaga) o arquivo ilegível ou VELHO: se a
    última observação de adiamento (`ultimo`) está a mais de `lacuna_s` do agora, o loop
    ficou fora do ar tempo demais para afirmar que é o MESMO episódio — começar do zero é
    honesto (senão o aviso diria "adiando há 10 h" sobre horas que ninguém observou)."""
    if not path:
        return {}
    lacuna_s = _CARGA_EPISODIO_LACUNA_S if lacuna_s is None else lacuna_s
    try:
        with open(path) as f:
            dados = json.load(f)
        desde = float(dados["desde"])
        ultimo = float(dados.get("ultimo", desde))
        escalado = bool(dados.get("escalado", False))
    except FileNotFoundError:
        return {}
    except Exception:
        log.warning("episódio de sobrecarga persistido ilegível em %s — descartado", path)
        _apagar_episodio_carga(path)
        return {}
    valido = (desde <= agora + 300.0 and desde <= ultimo <= agora + 300.0
              and (lacuna_s <= 0 or agora - ultimo <= lacuna_s))
    if not valido:
        _apagar_episodio_carga(path)
        return {}
    return {"desde": desde, "ultimo": ultimo, "escalado": escalado}


def _gravar_episodio_carga(path, estado_carga) -> None:
    """Grava {desde, ultimo, escalado} (troca atômica). Best-effort: falha vira WARNING no
    loop.err (a captura segue; só a sobrevivência ao reinício fica comprometida)."""
    if not path:
        return
    try:
        pasta = os.path.dirname(path)
        if pasta:
            os.makedirs(pasta, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump({"desde": estado_carga.get("desde"),
                       "ultimo": estado_carga.get("ultimo", estado_carga.get("desde")),
                       "escalado": bool(estado_carga.get("escalado"))}, f)
        os.replace(tmp, path)
    except Exception as e:
        log.warning("não gravei o episódio de sobrecarga em %s (%s: %s) — um reinício "
                    "zeraria o relógio do aviso", path, type(e).__name__, str(e)[:120])


def _apagar_episodio_carga(path) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("não apaguei o episódio de sobrecarga em %s (%s)", path,
                    type(e).__name__)


def _carregar_estado_cursos(path, *, agora) -> dict:
    """Estado por-curso PERSISTIDO por uma encarnação anterior (só as chaves da lista
    branca), ou {}. Nunca levanta: um arquivo ilegível/corrompido vira WARNING e {} —
    perder o backoff é ruim, não subir o daemon é pior.

    SANEAMENTO por entrada (o arquivo é dado, não código): curso tem de ser string
    não-vazia; o valor, um dict; cada chave, um número finito; instantes no futuro
    além de `_ESTADO_FUTURO_MAX_S` são descartados (relógio/arquivo corrompido não
    pode bloquear um curso por anos); `disj_falhas` vira int >= 0. Chave inválida é
    descartada SOZINHA — o resto da entrada sobrevive."""
    if not path:
        return {}
    try:
        with open(path) as f:
            dados = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        log.warning("estado de curso persistido ilegível em %s — descartado (a escada "
                    "do disjuntor recomeça do zero nesta encarnação)", path)
        return {}
    if not isinstance(dados, dict):
        log.warning("estado de curso persistido com formato inesperado em %s "
                    "(%s) — descartado", path, type(dados).__name__)
        return {}
    cursos = dados.get("cursos")
    if not isinstance(cursos, dict):
        return {}
    limite = agora + _ESTADO_FUTURO_MAX_S
    saida = {}
    for curso, st in cursos.items():
        if not isinstance(curso, str) or not curso or not isinstance(st, dict):
            continue
        limpo = {}
        for chave in _ESTADO_CURSOS_CHAVES:
            if chave not in st:
                continue
            valor = st[chave]
            if isinstance(valor, bool) or not isinstance(valor, (int, float)):
                continue                               # bool/str/None/lista: descarta
            try:
                valor = float(valor)                   # int gigante -> OverflowError
            except (OverflowError, ValueError, TypeError):
                continue
            if valor != valor or valor in (float("inf"), float("-inf")):
                continue                               # NaN/inf
            if chave in ("disj_falhas", "ultimo_no_notion", "_pend_no_cooldown"):
                if valor < 0:
                    continue
                limpo[chave] = int(valor)
            else:                                      # instantes (epoch)
                if valor > limite:
                    log.warning("estado persistido de %s: %s=%s está longe demais no "
                                "futuro — descartado", curso, chave, valor)
                    continue
                limpo[chave] = valor
        if limpo:
            saida[curso] = limpo
    return saida


def _gravar_estado_cursos(path, estado) -> None:
    """Grava as chaves da lista branca de cada curso (troca atômica). Best-effort: uma
    falha vira WARNING no loop.err — a captura segue, só a sobrevivência da escada ao
    reinício fica comprometida."""
    if not path:
        return
    try:
        cursos = {}
        for curso, st in (estado or {}).items():
            if not isinstance(st, dict):
                continue
            linha = {k: st[k] for k in _ESTADO_CURSOS_CHAVES
                     if isinstance(st.get(k), (int, float))
                     and not isinstance(st.get(k), bool)}
            if linha:
                cursos[curso] = linha
        pasta = os.path.dirname(path)
        if pasta:
            os.makedirs(pasta, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump({"versao": 1, "gravado_em": time.time(), "cursos": cursos}, f)
        os.replace(tmp, path)
    except Exception as e:
        log.warning("não gravei o estado dos cursos em %s (%s: %s) — um reinício zeraria "
                    "a escada do disjuntor", path, type(e).__name__, str(e)[:120])


def _avisar_adiamentos(adiados, *, alertas, espinha, agora, estado_carga,
                       limiar_s=None, episodio_path=None):
    """Aviso AGREGADO do portão de carga, no MÁXIMO 1 por ciclo (nunca 1 por curso).

    `adiados` = [(curso, motivo, escalonamento)] deste ciclo. Ciclo sem adiamento POR
    SOBRECARGA fecha o episódio — inclusive o ciclo só de RAMPA (escalonamento: a máquina
    já aliviou; o aviso de persistência não pode dizer "sobrecarregada há 1 h" sobre ele).
    Com sobrecarga: `alertas.maquina_sobrecarregada` SÓ-LOG enquanto o episódio é curto; se
    ele PERSISTE por `limiar_s` (ATHENA_CARGA_ALERTA_S), vira ESSENCIAL (a chave única deixa
    o dedup do Alertas em 1 ping por janela) + UMA escalada na espinha por episódio — o
    dono tem de saber que nada está sendo capturado (nunca "0 aulas" em silêncio).
    `estado_carga` (dict cross-ciclo do `rodar`) guarda {desde, ultimo, escalado}; com
    `episodio_path` o episódio vai também para DISCO (abre/atualiza/fecha), e o `rodar` o
    retoma no boot — o relógio do aviso atravessa reinícios. Best-effort."""
    limiar_s = _CARGA_ALERTA_S if limiar_s is None else limiar_s
    sobrecarga = [a for a in adiados if not _escalonado(a)]
    escalonados = [a for a in adiados if _escalonado(a)]
    aviso = getattr(alertas, "maquina_sobrecarregada", None)
    if not sobrecarga:
        if "desde" in estado_carga or "escalado" in estado_carga:
            _apagar_episodio_carga(episodio_path)
        estado_carga.pop("desde", None)
        estado_carga.pop("ultimo", None)
        estado_carga.pop("escalado", None)
        if escalonados and callable(aviso):
            try:                                            # rampa: só-log, sempre
                aviso(f"{len(escalonados)} disparo(s) escalonado(s) neste ciclo — "
                      f"{escalonados[-1][1]}", essencial=False,
                      chave=("portao_carga", "escalonamento"))
            except Exception:
                pass
        return
    desde = estado_carga.setdefault("desde", agora)
    estado_carga["ultimo"] = agora
    duracao = max(agora - desde, 0.0)
    persistente = limiar_s > 0 and duracao >= limiar_s
    motivo = sobrecarga[-1][1]
    texto = f"{len(adiados)} disparo(s) adiado(s) neste ciclo — {motivo}"
    if duracao > 0:
        texto += f" (adiando há {int(duracao // 60)} min)"
    if callable(aviso):
        try:
            aviso(texto, essencial=persistente, chave=("portao_carga",))
        except Exception:
            pass                                            # observabilidade nunca derruba
    if persistente and not estado_carga.get("escalado"):
        estado_carga["escalado"] = True
        _registrar(espinha, f"máquina sobrecarregada há {int(duracao // 60)} min — "
                   f"{len(adiados)} disparo(s) de captura adiado(s)",
                   f"{motivo}. Nada novo é capturado enquanto durar; retomo sozinho quando "
                   f"a carga baixar", tipo="escalada", reversivel=True, escalada=True,
                   fonte="guard", origem="athena-local/portao-carga")
    # DISCO: a cada ciclo de sobrecarga (abre, `ultimo` e `escalado`) — um JSON minúsculo
    # a cada ~2 min, só enquanto a sobrecarga dura.
    _gravar_episodio_carga(episodio_path, estado_carga)


def ciclo_local(cursos, executor, progresso_fn, voz, voo, estado, *, agora=None,
                plataformas_suportadas=None, max_tentativas=3, projeto_nome="athena-local",
                controle=None, controle_path=None, disjuntor=None, vigia=None, causa=None,
                alertas=None, lock_dir=None, autopsia_dir=None, meta_por_curso=None,
                flap_min=None, llm=None, espinha=None, sistemas=None,
                sistema_executor=None, causa_sistema=None, estado_sistemas=None,
                sistema_lock_dir=None, sistema_autopsia_dir=None,
                gasto_por_sistema_fn=None, budget_modo=None, pendencia_fn=None,
                pulsar=None, boot_ts=None, estado_carga=None, carga_episodio_path=None,
                zelador=None):
    """UM ciclo doméstico. Ordem: (1) CONTROLE filtra plataformas/contas PAUSADAS (P5,
    lido a cada volta); (2) gate de PLATAFORMA-NOVA pula cursos sem adaptador; (3) AUTÓPSIA
    dos cursos que morreram desde o último ciclo (P3->P4); (4) delega os demais ao OWNER
    `orquestrar_captura` com a passada LOCAL (disjuntor no gate de re-tentativa).

    A serialização anti-ban 1-por-conta é EMERGENTE (o executor levanta `ContaOcupada`). A
    contagem-verdade do Notion é lida UMA vez por curso por ciclo (cache).

    `pulsar(fase, curso=None)` (fix-livelock-loop): batida de PROGRESSO chamada no início,
    antes de cada escalada de plataforma-nova, antes da autópsia, ANTES DE CADA CURSO e
    antes de cada sistema. Um ciclo é SERIAL (42 cursos x leitura do Notion de até 120s +
    escalada no Telegram de até 30s): sem isto o pulso só existia no FIM do ciclo e o vigia
    externo (limiar 900s) matava um loop que estava AVANÇANDO. A batida é SÍNCRONA e ligada
    ao progresso (nunca uma thread independente): se uma chamada travar de verdade, o
    pulso CONGELA apontando a fase/curso — o livelock real continua detectável.

    `estado_carga` (dict cross-ciclo, criado 1x pelo `rodar`): episódio de sobrecarga do
    PORTÃO DE CARGA (ver `_avisar_adiamentos`). None => um dict local (sem escalada por
    persistência entre chamadas avulsas). `carga_episodio_path`: o mesmo episódio em DISCO
    (sobrevive a reinício; None = só memória). Antes das passadas o ciclo avisa o executor
    (`novo_ciclo`) — é a fronteira da janela do ESCALONAMENTO do portão de carga.

    `zelador` (P7, `maestro.zelador.Zelador` | None — DESLIGADO por padrão, ligado só por
    ATHENA_ZELADOR_ATIVO): passo (4b), DEPOIS da captura — ela tem prioridade no lock da
    conta. Só zela conta cujo curso passou gate/controle neste ciclo. Best-effort: um erro
    no zelador nunca derruba o ciclo nem mexe na captura."""
    def _bater(fase, curso=None):
        if pulsar is None:
            return
        try:
            pulsar(fase, curso)
        except Exception:
            pass                                           # observabilidade NUNCA derruba o ciclo

    _bater("inicio")
    if agora is None:
        agora = time.time()
    controle = controle or _ControleNulo()
    disjuntor = disjuntor or _DisjuntorTeto(max_tentativas)
    vigia = vigia or _VigiaNulo()
    causa = causa or _CausaNula()
    alertas = alertas or _AlertasNulo()
    espinha = espinha or _EspinhaNula()
    if flap_min is None:
        flap_min = _FLAP_MIN_PADRAO
    if meta_por_curso is None:
        meta_por_curso = {c.url: c for c in cursos}

    cache = {}

    def progresso_cached(curso):
        if curso not in cache:
            try:
                cache[curso] = ("ok", progresso_fn(curso))
            except Exception as e:                         # erro cacheado: 1 leitura/ciclo
                cache[curso] = ("err", e)
        kind, val = cache[curso]
        if kind == "err":
            raise val
        return val

    # (1) CONTROLE (P5): remove cursos de plataforma/conta PAUSADA — o daemon PARA de
    # RE-disparar aquela plataforma no próximo ciclo. Processo VIVO nunca é morto (Ordem IV).
    # `controle.yaml` corrompido => `filtrar_cursos` LEVANTA (fail-LOUD) e o ciclo estoura
    # (o `rodar` escala honesto). Só filtra quando há um path de controle (senão: passthrough).
    if controle_path is not None:
        cursos = controle.filtrar_cursos(cursos, controle_path)

    # (2) gate de PLATAFORMA-NOVA.
    cursos_ok = []
    for c in cursos:
        st = estado.setdefault(c.url, {})
        if (plataformas_suportadas is not None and
                not adaptador_pipeline.plataforma_suportada(c.url, plataformas_suportadas)):
            # a escalada vai ao Telegram (até 30s/tentativa com a rede ruim — 07/08 11:32
            # mostra 30s entre cada 'plataforma nova'): bate ANTES de cada uma.
            _bater("gate", c.url)
            _escalar_plataforma_nova(projeto_nome, voz, c.url, st, espinha=espinha)
            continue
        cursos_ok.append(c.url)

    # (3) AUTÓPSIA dos mortos (P3 -> P4): antes das passadas, para que a decisão da causa
    # (backoff vs irredutível) já esteja no `st` quando a passada consultar o disjuntor.
    _bater("autopsia")
    _autopsiar_ciclo(executor, estado, vigia=vigia, causa=causa, disjuntor=disjuntor,
                     alertas=alertas, lock_dir=lock_dir, autopsia_dir=autopsia_dir,
                     agora=agora, meta_por_curso=meta_por_curso, flap_min=flap_min,
                     llm=llm, espinha=espinha, boot_ts=boot_ts)

    # (4) passada LOCAL + owner. FRONTEIRA DE CICLO do portão de carga: a janela do
    # ESCALONAMENTO (no máx N disparos novos por ciclo na saída da sobrecarga) vira AQUI,
    # antes do 1º disparo do ciclo. Best-effort (dublês/executores sem o gancho: no-op).
    _novo = getattr(executor, "novo_ciclo", None)
    if callable(_novo):
        try:
            _novo()
        except Exception:
            pass
    adiados = []                              # portão de carga: (curso, motivo, escalonamento)
    passada_do_curso = _passada_local_fn(executor, progresso_cached, voz, estado,
                                         projeto_nome=projeto_nome, disjuntor=disjuntor,
                                         agora=agora, espinha=espinha,
                                         pendencia_fn=pendencia_fn, adiados=adiados)

    def passada(curso):
        # batida ANTES de cada curso: a janela entre duas batidas é UM curso (leitura do
        # Notion <=120s + escalada), não o ciclo inteiro (N cursos).
        _bater("passada", curso)
        acao = passada_do_curso(curso)
        # ADIADO PELO PORTÃO DE CARGA neste ciclo: o relógio de STALL do owner NÃO corre —
        # o curso não empacou, a Athena é que o segurou. Sem isto, 30 min de sobrecarga
        # viravam um "captura ESTAGNADA" POR CURSO (o alerta por curso que o portão não
        # pode gerar); a sobrecarga longa é avisada UMA vez, agregada.
        if estado.get(curso, {}).get("_adiado_ciclo") == agora:
            info = voo.get(curso)
            if info is not None:
                info["desde"] = agora
        return acao

    def notion_fn(curso):
        try:
            no_notion, total = progresso_cached(curso)
        except Exception:
            return (None, 0)
        return (no_notion, total)

    resultado_captura = orquestrador.orquestrar_captura(cursos_ok, passada, notion_fn,
                                                        voz, voo, agora=agora)
    _avisar_adiamentos(adiados, alertas=alertas, espinha=espinha, agora=agora,
                       estado_carga=estado_carga if estado_carga is not None else {},
                       episodio_path=carga_episodio_path)

    # (4a) VIGIA PERSISTIDO DOS ZELOS ÓRFÃOS (P7): roda em TODO ciclo, com o zelador ligado
    # ou não — um restart no meio de um zelo (inclusive o rollback ATHENA_ZELADOR_ATIVO=0 +
    # restart) deixa um lock `dono: zelador` que só este vigia mata/limpa (o do executor é
    # em memória). Best-effort: nunca derruba o ciclo.
    vigiar_orfaos = getattr(executor, "vigiar_zelos_orfaos", None)
    if callable(vigiar_orfaos):
        try:
            vigiar_orfaos(agora)
        except Exception:
            pass

    # (4b) ZELADOR DE SESSÃO (P7): contas OCIOSAS têm a sessão provada pela sonda da própria
    # plataforma, segurando o MESMO lock durável da conta (nunca junto de captura). Depois
    # da passada: quem disparou captura neste ciclo já está com a conta travada.
    if zelador is not None:
        _bater("zelador")
        try:
            zelador.passo(executor, estado, agora=agora, alertas=alertas, espinha=espinha,
                          ativos=frozenset(cursos_ok))
        except Exception:
            pass                                           # o zelo NUNCA derruba o ciclo

    # (5) SISTEMAS GERADOS (F4-d): supervisão ao lado da captura. A captura acima NÃO
    # muda em nada; sem sistemas registrados (default) este passo é um no-op — o
    # padrão P1–P6 preservado (zero regressão na captura). Best-effort por sistema:
    # um erro na supervisão de um sistema não derruba o ciclo nem afeta a captura.
    if sistemas and sistema_executor is not None:
        causa_sistema = causa_sistema or _CausaNula()
        if estado_sistemas is None:
            estado_sistemas = {}
        for spec in sistemas:
            st_s = estado_sistemas.setdefault(spec.slug, {})
            _bater("sistema", spec.slug)
            try:
                acao = passada_sistema(
                    spec, st_s, sistema_executor, vigia=vigia,
                    causa_sistema=causa_sistema, disjuntor=disjuntor, alertas=alertas,
                    espinha=espinha, agora=agora,
                    lock_dir=sistema_lock_dir, autopsia_dir=sistema_autopsia_dir,
                    flap_min=flap_min, llm=llm,
                    gasto_por_sistema_fn=gasto_por_sistema_fn, budget_modo=budget_modo)
            except Exception:
                acao = None                            # supervisão nunca derruba o ciclo
            if acao is not None:
                voz.avisar_acao(acao)                  # só posta se acao.executada

    return resultado_captura


# Fatia máxima da espera ENTRE ciclos sem regravar o pulso. Bem abaixo do limiar de 900s do
# vigia externo: com MAESTRO_INTERVALO_S alto (>900) o loop seria morto DORMINDO. No
# intervalo de produção (120s) é uma fatia só — o sono não muda.
_PULSO_FATIA_SONO_S = 300.0


def _escrever_pulso(path, *, ts, ciclo, ativos, fase=None, curso=None):
    """Grava o PULSO de vida do daemon de forma atômica (tmp+replace). É a prova EXTERNA
    (fora da memória do processo) de que o loop está VIVO e avançando — o que o VIGIA
    EXTERNO (P6, scripts/vigia_externo.sh) lê para decidir se a Athena travou.

    Contrato original {ts, ciclo, ativos} INTACTO (o vigia lê `ts`). fix-livelock-loop soma
    INSTRUMENTAÇÃO: `pid` (qual encarnação escreveu — um pulso de outro pid é órfão de uma
    encarnação anterior), `fase` (boot|inicio|gate|autopsia|passada|sistema|fim|dormindo) e
    `curso` (o alvo da fase). Quando o pulso PARA, ele diz ONDE o loop parou."""
    import json
    dados = {"ts": ts, "ciclo": ciclo, "ativos": list(ativos), "pid": os.getpid()}
    if fase is not None:
        dados["fase"] = fase
    if curso is not None:
        dados["curso"] = curso
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(dados, f)
    os.replace(tmp, path)


async def rodar(cursos, executor, progresso_fn, voz, *, sleep=asyncio.sleep,
                intervalo_s=120.0, max_iters=None, plataformas_suportadas=None,
                max_tentativas=3, projeto_nome="athena-local", controle=None,
                controle_path=None, disjuntor=None, vigia=None, causa=None, alertas=None,
                batimento=None, batimento_intervalo=1800.0, resumo_fn=None, pulso_path=None,
                lock_dir=None, autopsia_dir=None, meta_por_curso=None, flap_min=None,
                llm=None, espinha=None, sistemas=None, sistema_executor=None,
                causa_sistema=None, sistema_lock_dir=None, sistema_autopsia_dir=None,
                gasto_por_sistema_fn=None, budget_modo=None, pendencia_fn=None,
                reaper_fn=None, boot_ts=None, carga_episodio_path=None, zelador=None,
                estado_cursos_path=None):
    """O LOOP doméstico. Cria `voo` e `estado` UMA vez e os REINJETA a cada ciclo. Um ciclo
    que estoura NÃO derruba o loop, mas a falha é ESCALADA (latch por assinatura). O PULSO
    é gravado no BOOT, em cada fase/curso do ciclo, no fim do ciclo e a cada fatia da espera
    (fix-livelock-loop); o BATIMENTO roda a cada ciclo — ambos BEST-EFFORT (observabilidade
    nunca mata o loop). SISTEMAS gerados (F4-d) entram como alvos supervisionados no passo
    (5) do ciclo — `estado_sistemas` persiste entre ciclos, igual ao `estado` dos cursos.

    `carga_episodio_path` (achado r6): o episódio de sobrecarga do portão de carga em DISCO.
    No boot, um episódio ainda aberto (e não velho — ver `_carregar_episodio_carga`) é
    RETOMADO: o relógio do aviso ESSENCIAL continua de onde parou e o portão nasce em
    sobrecarga (`executor.retomar_sobrecarga`: a histerese atravessa o reinício).

    `estado_cursos_path` (achado r15, ANTI-BAN): o `estado` por-curso em DISCO. No boot,
    a escada do disjuntor e os cooldowns de uma encarnação anterior são RESTAURADOS
    (lista branca em `_ESTADO_CURSOS_CHAVES`) e, ao fim de CADA ciclo, regravados. Sem
    isso, qualquer reinício zerava um cooldown de até 24 h e o curso voltava a ser
    martelado. None = só memória (o comportamento antigo)."""
    estado = {}
    voo = {}
    estado_sistemas = {}
    estado_carga = {}                  # episódio do portão de carga (retomado após o pulso)
    controle = controle or _ControleNulo()
    disjuntor = disjuntor or _DisjuntorTeto(max_tentativas)
    vigia = vigia or _VigiaNulo()
    causa = causa or _CausaNula()
    alertas = alertas or _AlertasNulo()
    batimento = batimento or _BatimentoNulo()
    espinha = espinha or _EspinhaNula()
    if meta_por_curso is None:
        meta_por_curso = {c.url: c for c in cursos}
    ultimo_erro = None
    ultimo_batimento = 0.0
    t0 = time.time()
    # PULSO DE PROGRESSO (fix-livelock-loop). Antes: UM pulso por ciclo, gravado só DEPOIS
    # do ciclo inteiro — nada no boot, nada durante. Evidência: em 07/08 11:32→11:52 o vigia
    # matou 4 loops recém-nascidos seguidos, cada um preso no 1º curso do 1º ciclo (Notion
    # estourando 120s) com o pulso ainda da encarnação ANTERIOR; o 10/09 16:29 matou um loop
    # de ~20s de vida por um pulso de 6 dias. Agora o pulso é regravado no BOOT, em cada fase
    # e ANTES DE CADA CURSO. `ativos` só é recalculado nos pulsos CHEIOS (boot/fim de ciclo)
    # — as batidas de progresso reusam o último snapshot: ZERO chamada nova ao executor no
    # meio do ciclo. Tudo best-effort (observabilidade nunca derruba o loop).
    ultimos_ativos = []

    def _pulsar(ciclo, fase, curso=None, *, cheio=False):
        nonlocal ultimos_ativos
        if not pulso_path:
            return
        try:
            if cheio:
                ultimos_ativos = [c.url for c in cursos if _safe_ativo(executor, c.url)]
            _escrever_pulso(pulso_path, ts=time.time(), ciclo=ciclo, ativos=ultimos_ativos,
                            fase=fase, curso=curso)
        except Exception:
            pass

    # 1º ATO da encarnação: pulsar — ANTES do reaper e do 1º ciclo. O pulso órfão de uma
    # encarnação anterior (morta/pré-reboot) deixa de valer AGORA, não ao fim do 1º ciclo.
    # Pulso MAGRO (cheio=False, `ativos` vazio até o 1º pulso cheio de "fim"): o cheio
    # chama executor.curso_ativo -> LocalExecutor._ler_lock, que APAGA lock de PID morto.
    # No boot esse lock é a ÚNICA evidência da morte da encarnação anterior, e a 1ª
    # varredura de autópsia (vigia.autopsia(lock_dir), fase "autopsia" do 1º ciclo) ainda
    # não rodou: apagá-lo aqui era perder a autópsia (achado da revisão do livelock).
    _pulsar(0, "boot")
    # EPISÓDIO DE SOBRECARGA de uma encarnação anterior (achado r6), logo DEPOIS do pulso de
    # boot: o relógio do aviso ESSENCIAL continua, e o portão nasce em sobrecarga (a
    # histerese atravessa o reinício — senão a 1ª leitura entre os dois limites liberava
    # todas as contas livres de uma vez). Best-effort: nunca impede o loop de subir.
    try:
        estado_carga.update(_carregar_episodio_carga(carga_episodio_path,
                                                     agora=time.time()))
    except Exception:
        pass
    # ESTADO POR-CURSO de uma encarnação anterior (achado r15): a escada do disjuntor e
    # os cooldowns voltam ANTES do 1º ciclo — senão o reinício liberava para martelar um
    # curso que estava de castigo por até 24 h. Best-effort: nunca impede o loop de subir.
    try:
        estado.update(_carregar_estado_cursos(estado_cursos_path, agora=time.time()))
    except Exception:
        log.warning("não restaurei o estado dos cursos — a escada do disjuntor "
                    "recomeça do zero nesta encarnação", exc_info=True)
    if "desde" in estado_carga:
        _retomar = getattr(executor, "retomar_sobrecarga", None)
        if callable(_retomar):
            try:
                _retomar()
            except Exception:
                pass
    # HIGIENE (1) — REAPER NO BOOT DA LANE: ANTES do 1º ciclo, devolve a `pendente` as
    # aulas in-flight async órfãs (transcrevendo*/capturando_nao_video) de uma encarnação
    # ANTERIOR cujo processo NÃO está mais vivo (crash/redeploy). Sem isso elas ficam presas
    # para sempre. Best-effort: a higiene NUNCA pode impedir o loop de subir. Gate anti-ban
    # é do próprio reaper (só toca curso SEM captura viva — ver captura.reap_orphans_local).
    # O reaper também roda ANTES da 1ª autópsia: o de produção (`_reaper_de_boot`) sonda a
    # liveness em modo SÓ-LEITURA pelo mesmo motivo do pulso de boot acima.
    if reaper_fn is not None:
        try:
            reaper_fn()
        except Exception:
            pass
    i = 0
    while max_iters is None or i < max_iters:
        i += 1

        def _bater(fase, curso=None, _ciclo=i):
            _pulsar(_ciclo, fase, curso)

        try:
            ciclo_local(cursos, executor, progresso_fn, voz, voo, estado,
                        agora=time.time(), plataformas_suportadas=plataformas_suportadas,
                        max_tentativas=max_tentativas, projeto_nome=projeto_nome,
                        controle=controle, controle_path=controle_path, disjuntor=disjuntor,
                        vigia=vigia, causa=causa, alertas=alertas, lock_dir=lock_dir,
                        autopsia_dir=autopsia_dir, meta_por_curso=meta_por_curso,
                        flap_min=flap_min, llm=llm, espinha=espinha, sistemas=sistemas,
                        sistema_executor=sistema_executor, causa_sistema=causa_sistema,
                        estado_sistemas=estado_sistemas, sistema_lock_dir=sistema_lock_dir,
                        sistema_autopsia_dir=sistema_autopsia_dir,
                        gasto_por_sistema_fn=gasto_por_sistema_fn, budget_modo=budget_modo,
                        pendencia_fn=pendencia_fn, pulsar=_bater, boot_ts=boot_ts,
                        estado_carga=estado_carga, carga_episodio_path=carga_episodio_path,
                        zelador=zelador)
            ultimo_erro = None                             # ciclo passou: re-arma o latch
        except Exception as e:
            assinatura = f"{type(e).__name__}:{str(e)[:120]}"
            if assinatura != ultimo_erro:                  # episódio novo -> escala honesto
                pedido = (f"[{projeto_nome}] o CICLO doméstico ESTOUROU (não derrubo o "
                          f"loop, mas NÃO capturo nada até resolver): {assinatura}")
                try:
                    voz.escalar(Problema("ciclo_local_estourou", projeto_nome, pedido,
                                         "critico"), pedido)
                except Exception:
                    pass                                   # a voz falhar não pode matar o loop
                # E13: ciclo doméstico estourou (latch por assinatura) — escala, o
                # loop NÃO morre. A espinha é fail-safe: um erro AQUI também não mata.
                _registrar(espinha, "o CICLO doméstico estourou",
                           assinatura, tipo="escalada", reversivel=True,
                           escalada=True, fonte="fail-closed", origem="athena-local/rodar")
                ultimo_erro = assinatura
        # ESTADO POR-CURSO EM DISCO (achado r15) — gravado ao fim de CADA ciclo, INCLUSIVE
        # num ciclo que estourou (o `estado` é mutado in-place pela passada, então o que
        # já foi decidido neste ciclo — um cooldown armado, uma falha no disjuntor — tem de
        # sobreviver ao reinício que o vigia externo pode causar em seguida). Best-effort:
        # a função já engole a exceção e loga WARNING.
        _gravar_estado_cursos(estado_cursos_path, estado)
        # PULSO CHEIO de fim de ciclo (P6 backstop lê isto) — best-effort, gravado MESMO
        # num ciclo que estourou. Recalcula `ativos` (o snapshot das batidas seguintes).
        _pulsar(i, "fim", cheio=True)
        # BATIMENTO (P2) — pulso de vida no Telegram (mudo: só loga). Best-effort.
        def _resumo():
            if resumo_fn is not None:
                return resumo_fn()
            n = sum(1 for c in cursos if _safe_ativo(executor, c.url))
            return f"{n} captura(s) ativa(s), ciclo {i}, uptime {int(time.time() - t0)}s"
        try:
            ultimo_batimento = batimento.talvez_bater(
                voz, _resumo, time.time(), ultimo_batimento, batimento_intervalo)
        except Exception:
            pass
        # ESPERA entre ciclos, FATIADA: nenhuma fatia passa de _PULSO_FATIA_SONO_S sem
        # regravar o pulso. Com o intervalo de produção (120s) é UMA chamada, idêntica.
        restante = float(intervalo_s)
        while True:
            fatia = min(restante, _PULSO_FATIA_SONO_S)
            await sleep(fatia)
            restante -= fatia
            if restante <= 0:
                break
            _pulsar(i, "dormindo")
    return i


# ---------------------------------------------------------------------------
# Contagem-verdade do Notion LOCAL (I/O real; testada pelo seam `run`).
# ---------------------------------------------------------------------------
def _run_local(cmd, *, cwd):  # pragma: no cover — subprocesso REAL
    import subprocess
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=120).stdout


def contar_no_notion_local(motor_python, motor_dir, curso_url, *, run=None,
                           prefixo=None) -> int:
    """Conta as aulas deste curso já no Notion (por prefixo de 'Origem'), rodando o
    MESMO script do adaptador (`captura._PROGRESSO_SCRIPT`) LOCALMENTE (motor_python -c),
    com cwd=motor_dir para o `motor.config` carregar o NOTION_TOKEN do .env. LEVANTA se a
    sentinela não vier (silêncio NÃO vira 0 — subestimar re-capturaria; o chamador escala).
    `prefixo` (opcional) = a Origem da plataforma quando ela não mora sob a URL do curso
    (`captura.prefixo_origem_notion` — Entrega Digital); None => o prefixo da URL."""
    run = run or _run_local
    prefixo = prefixo or captura._prefixo_de_curso(curso_url)
    script = "import motor.config  # carrega .env (NOTION_TOKEN etc.)\n" + captura._PROGRESSO_SCRIPT
    cmd = [motor_python, "-c", script, prefixo]
    saida = run(cmd, cwd=motor_dir) or ""
    if captura.PROGRESSO_SENTINELA not in saida:
        raise RuntimeError(
            f"contagem LOCAL no Notion SEM confirmação ({captura.PROGRESSO_SENTINELA} "
            f"ausente) p/ {curso_url}: {saida[-160:]!r}")
    linha = next(l for l in saida.splitlines() if captura.PROGRESSO_SENTINELA in l)
    return int(linha.split(captura.PROGRESSO_SENTINELA, 1)[1].strip().split()[0])


def progresso_local_fn(motor_python, motor_dir, total_por_curso, *, run=None,
                       origem_por_curso=None):
    """Fábrica da `progresso_fn` doméstica -> (no_notion, total). Numerador = contagem-
    verdade LOCAL do Notion; denominador = `total_por_curso` (o `total_esperado` do YAML).
    Curso sem total => 0 (o owner não conclui: fail-closed). `origem_por_curso` (url ->
    prefixo de Origem) cobre as plataformas cuja aula não mora sob a URL do YAML."""
    origem = dict(origem_por_curso or {})

    def _fn(curso_url):
        no_notion = contar_no_notion_local(motor_python, motor_dir, curso_url, run=run,
                                           prefixo=origem.get(curso_url))
        return (no_notion, int(total_por_curso.get(curso_url, 0)))
    return _fn


def origem_notion_por_curso(cursos) -> dict:
    """url -> prefixo de 'Origem' no Notion, só dos cursos cuja plataforma grava a aula
    FORA da URL do YAML (`captura.prefixo_origem_notion`; hoje: Entrega Digital). O main()
    passa isto ao `progresso_local_fn` — sem ele o progresso da Entrega Digital seria 0
    para sempre (nenhum avanço visto)."""
    out = {}
    for c in cursos:
        p = captura.prefixo_origem_notion(c.url, c.plataforma)
        if p:
            out[c.url] = p
    return out


# Gate de DOMÍNIO default (main() -> `plataformas_suportadas`, casada por sufixo de host
# em `adaptador_pipeline.plataforma_suportada`). Fora desta lista => o ciclo NÃO despacha
# (Capacidade B: plataforma nova escala, nunca captura às cegas). ADITIVO: os 4 primeiros
# são as plataformas vivas de sempre; os 5 seguintes são os adaptadores novos fiados em
# 22/07 (kiwify/nutror/alpaclass/hubla/greenn — sessões semeadas 20/07); o último é o
# Curseduca (white-label — o host é DO TENANT, não da plataforma: segueadi, provado ao
# vivo 24/07; um tenant Curseduca novo = host novo AQUI + entrada YAML com `tenant:`).
# GREENN: o CLUB (onde o aluno assiste e de onde o motor.greenn enumera) vive em
# `<tenant>.greenn.club` (ex.: ytubeclass.greenn.club; API em api.greenn.club — ver
# motor/greenn/cli.py). `greenn.com.br` é o painel do PRODUTOR (adm.greenn.com.br), não
# o club: com ele no default, nenhuma URL de curso Greenn passava o gate (casa por
# sufixo) e uma URL do painel passaria. Por isso o sufixo é `greenn.club`.
# CADEMÍ (rodada 9): cada tenant é DOMÍNIO PRÓPRIO sem sufixo comum (CNAME para
# core.cademi.com.br) — entram os 3 hosts EXATOS (o gate aceita host igual; subdomínio
# deles também casa, prefixo/sufixo parecido não). ENTREGA DIGITAL: `<tenant>.
# entregadigital.app.br`. Nenhum dispara sem entrada no YAML; e o launch.sh vivo fixa a
# env PLATAFORMAS_SUPORTADAS (sem estes 4) — ligar = acrescentá-los lá também. Host no
# gate NÃO escolhe plataforma: a entrada sem `plataforma:` fora do hotmart.com é recusada
# no disparo (`captura.plataforma_padrao`) — tenant novo = host aqui/no launch.sh + linha
# `plataforma:` no YAML (+ o host em `hosts` do spec, para a trava contra linha errada).
# Env PLATAFORMAS_SUPORTADAS sobrepõe (ex.: para pausar uma plataforma sem tocar código).
PLATAFORMAS_SUPORTADAS_PADRAO = (
    "hotmart.com", "memberkit.com.br", "stoa.com.br", "mykajabi.com",
    "kiwify.com.br", "nutror.com", "alpaclass.com", "hub.la", "greenn.club",
    "membros.segueadi.com",
    "membros.alfaresearch.com.br", "cursos.codigoviral.com.br",
    "aulas.ramonpereira.com.br", "entregadigital.app.br")


def carregar_cursos(path) -> list:
    """Lê a lista de cursos desejados do YAML doméstico. Cada entrada:
    {url, conta, plataforma?, total_esperado?, session_path?, tenant?}. `conta` é
    OBRIGATÓRIA (chave anti-ban) — a ausência LEVANTA (fail-closed). `plataforma`
    ausente (ou vazia) => `captura.plataforma_padrao(url)`: "hotmart" SÓ num host
    hotmart.com; em qualquer outro host a entrada vira PLATAFORMA_NAO_DECLARADA e o
    disparo a recusa NOMEADA (nunca o motor Hotmart por omissão — um tenant novo de
    domínio próprio posto no gate sem a linha rodaria o `motor.cli` no `.chrome-profile`
    do Hotmart vivo). Não levanta aqui: uma entrada mal configurada não derruba o boot
    do daemon (as outras seguem); ela escala a cada ciclo. `session_path`
    (opcional) aponta o storage_state EXISTENTE da conta (semeado por login manual do
    usuário); só é INJETADO no motor pelas plataformas cujo spec declara `session_env`
    — nas demais é carregado mas inerte (comportamento vivo intacto). `tenant`
    (opcional, white-label — Curseduca) idem: só é injetado via `tenant_env` do spec,
    e VENCE o default fixado no spec (o caminho multi-tenant sem tocar código)."""
    import yaml
    with open(path) as f:
        dados = yaml.safe_load(f) or []
    out = []
    for d in dados:
        out.append(captura.CursoLocal(
            url=d["url"], conta=d["conta"],
            plataforma=(str(d.get("plataforma") or "").strip()
                        or captura.plataforma_padrao(d["url"])),
            total_esperado=int(d.get("total_esperado", 0)),
            session_path=str(d.get("session_path", "") or ""),
            tenant=str(d.get("tenant", "") or "")))
    return out


def _pendencia_de_producao(cursos, motor_dir_do_curso):
    """O `pendencia_fn` de PRODUÇÃO (main -> rodar): pendência capturável por curso
    (None = desconhecido -> fail-open), lida no tracker do motor DAQUELE curso e no
    ESCOPO da plataforma do YAML — Cademí/Entrega Digital somam o tenant inteiro pelo
    course_id namespaced (`captura._escopo_tracker`); sem a plataforma, o leitor só
    conhecia o `/products/<id>` do Hotmart (None para elas, ou o curso Hotmart errado)."""
    plat_por_curso = {c.url: c.plataforma for c in cursos}

    def pendencia_fn(url):
        return captura.pendencia_capturavel_local(
            url, motor_dir_do_curso(url), plataforma=plat_por_curso.get(url))
    return pendencia_fn


def _reaper_de_boot(cursos, executor, motor_dir_do_curso):
    """O `reaper_fn` de PRODUÇÃO (main -> rodar): devolve a `pendente` as aulas in-flight
    async órfãs de cada curso SEM captura viva (`captura.reap_orphans_local`).

    A liveness é sondada em modo SÓ-LEITURA (`executor.curso_ativo(url, limpar=False)`):
    o reaper roda no BOOT, ANTES da 1ª varredura de autópsia, e o `curso_ativo` padrão
    APAGA o lock de PID morto — a única evidência da morte da encarnação anterior (a
    autópsia sumia). A resposta do gate anti-ban é IDÊNTICA (vivo/intenção -> intocável;
    morto -> reapável); só o efeito colateral some. O lock morto é limpo depois da
    autópsia, pelo pulso cheio de fim de ciclo / pela passada. Best-effort por curso.
    A `plataforma` do YAML vai junto: Cademí/Entrega Digital não são reapadas (o motor
    retoma o próprio in-flight; e o course_id delas não é o `/products/` do Hotmart)."""
    def _vivo(url):
        return executor.curso_ativo(url, limpar=False)

    def reaper_fn():
        for c in cursos:
            try:
                captura.reap_orphans_local(c.url, motor_dir_do_curso(c.url),
                                           curso_ativo=_vivo, plataforma=c.plataforma)
            except Exception:
                pass
    return reaper_fn


def _motor_dirs_por_plataforma() -> dict:
    """Overrides de diretório do motor POR PLATAFORMA, lidos de env ATHENA_MOTOR_DIR_<PLAT>."""
    prefixo = "ATHENA_MOTOR_DIR_"
    return {k[len(prefixo):].lower(): v for k, v in os.environ.items()
            if k.startswith(prefixo) and v}


def _ler_groq_key(motor_dir) -> str:  # pragma: no cover — I/O real (lê o chave-groq.txt)
    """GROQ_API_KEY p/ INJETAR no subprocesso do motor (Stoa roda com cwd=worktree, sem
    chave-groq.txt lá). Lê de env GROQ_API_KEY ou do chave-groq.txt do `motor_dir`."""
    import re
    v = os.getenv("GROQ_API_KEY")
    if v:
        return v
    try:
        with open(os.path.join(motor_dir, "chave-groq.txt")) as f:
            m = re.search(r"gsk_[A-Za-z0-9]{20,}", f.read())
            return m.group(0) if m else ""
    except OSError:
        return ""


def _montar_alertas(cfg):  # pragma: no cover — I/O real (constrói o canal de supervisão)
    """Constrói os Alertas typados (P2) FAIL-SAFE: token MUDO/ausente => modo só-log (o
    canal que avisa que o supervisor caiu NUNCA pode ser quem o derruba). Com token real,
    posta no Telegram reusando o mesmo TelegramClient da Voz."""
    from maestro.alertas import Alertas
    from maestro.telegram_api import TelegramClient
    token = (cfg.bot_token or "").strip()
    if not token or token == "MUTED":
        return Alertas(None, list(cfg.chat_ids))
    return Alertas(TelegramClient(token), list(cfg.chat_ids))


def main():  # pragma: no cover — I/O real (monta os seams concretos e roda o loop)
    from maestro import batimento as batimento_mod
    from maestro import carga as carga_mod
    from maestro import causa as causa_mod
    from maestro import controle as controle_mod
    from maestro import decisoes as decisoes_mod
    from maestro import disjuntor as disjuntor_mod
    from maestro import vigia as vigia_mod
    from maestro.config import carregar
    from maestro.telegram_api import TelegramClient
    from maestro.voz import Voz

    cfg = carregar()
    voz = Voz(TelegramClient(cfg.bot_token), cfg.chat_ids)
    alertas = _montar_alertas(cfg)

    motor_python = os.getenv(
        "ATHENA_MOTOR_PYTHON", "/Users/guilhermerodrigues/teste/aula/.venv/bin/python")
    motor_dir = os.getenv("ATHENA_MOTOR_DIR", "/Users/guilhermerodrigues/teste/aula")
    cursos_path = os.environ["ATHENA_LOCAL_CURSOS"]
    cursos = carregar_cursos(cursos_path)

    lock_dir = os.getenv("ATHENA_LOCK_DIR") or None
    groq_key = _ler_groq_key(motor_dir) or None
    # PORTÃO DE CARGA (incidente 10/09): ATHENA_CARGA_MAX (default 1,5×núcleos),
    # ATHENA_MEM_LIVRE_MIN_PCT (default 10), ATHENA_MAX_MOTORES (default 0 = sem teto);
    # histerese ATHENA_CARGA_LIBERA (default 0,8×máx) / ATHENA_MEM_LIVRE_LIBERA_PCT
    # (default mín+5) e escalonamento ATHENA_CARGA_DISPAROS_POR_CICLO (default 2).
    portao_carga = carga_mod.PortaoCarga.do_ambiente()
    decisoes_mod.registrar_decisao(
        "portão de carga ligado", portao_carga.descrever(), reversivel=True,
        fonte="guard", origem="athena-local/main")
    # ZELADOR DE SESSÃO (P7): DESLIGADO por padrão. ATHENA_ZELADOR_ATIVO=seco => só grava
    # o status (~/.athena-local/sessoes-status.json ou ATHENA_SESSOES_STATUS), sem abrir
    # navegador; =1 => zela. A ativação é assistida (docs/P7 do motor). None = zero mudança.
    from maestro import zelador as zelador_mod
    zelador = zelador_mod.zelador_do_ambiente(cursos)
    if zelador is not None:
        decisoes_mod.registrar_decisao(
            f"zelador de sessão em modo {zelador.modo}", zelador.descrever(),
            reversivel=True, fonte="guard", origem="athena-local/main")
    executor = captura.LocalExecutor(
        cursos, motor_python=motor_python, motor_dir=motor_dir, lock_dir=lock_dir,
        groq_key=groq_key, motor_dir_por_plataforma=_motor_dirs_por_plataforma(),
        portao_carga=portao_carga,
        zelo_timeout_s=(zelador.cfg.timeout_s if zelador is not None
                        else captura._ZELO_TIMEOUT_PADRAO_S))
    total_por_curso = {c.url: c.total_esperado for c in cursos}
    progresso_fn = progresso_local_fn(motor_python, motor_dir, total_por_curso,
                                      origem_por_curso=origem_notion_por_curso(cursos))

    # HIGIENE — motor_dir POR CURSO (Stoa vive noutro worktree): o mesmo mapa que o
    # executor usa (`_motor_dir_de`), para que reaper/pendência leiam o tracker.db CERTO.
    _plat_por_curso = {c.url: c.plataforma for c in cursos}

    def _motor_dir_do_curso(url):
        return executor._motor_dir_de(_plat_por_curso.get(url, "hotmart"))

    # HIGIENE (2): pendência capturável por curso (None = desconhecido -> fail-open), no
    # escopo da plataforma do YAML (Cademí/Entrega Digital: o tenant inteiro).
    pendencia_fn = _pendencia_de_producao(cursos, _motor_dir_do_curso)

    # HIGIENE (1): reaper de órfãos async no BOOT da lane — só toca curso SEM captura viva
    # (gate anti-ban dentro de `reap_orphans_local`). Best-effort por curso.
    reaper_fn = _reaper_de_boot(cursos, executor, _motor_dir_do_curso)

    plataformas = frozenset(
        p for p in os.getenv(
            "PLATAFORMAS_SUPORTADAS", ",".join(PLATAFORMAS_SUPORTADAS_PADRAO))
        .replace(" ", "").split(",") if p)
    plataformas_suportadas = plataformas or None
    # ATHENA_MAX_TENTATIVAS: o LIMIAR de falhas consecutivas que ARMA a 1ª janela do
    # recozimento (3 = duas falhas "de graça", a terceira arma 600s). Até a r15 esta env
    # era código morto (ver `_DisjuntorRecozido`); agora ela é fiada no disjuntor REAL.
    # Valor inválido não derruba o daemon: WARNING e o default. Mínimo 1 (0/negativo
    # armaria a janela antes da 1ª falha e nenhum curso dispararia nunca).
    max_tentativas = _env_int("ATHENA_MAX_TENTATIVAS", 3, minimo=1)

    # Caminhos duráveis das 6 partes (todos sob ~/.athena-local por padrão, o mesmo home
    # do lock_dir anti-ban — sobrevive a reboot).
    base = os.path.join(os.path.expanduser("~"), ".athena-local")
    controle_path = os.getenv("ATHENA_CONTROLE_PATH", os.path.join(base, "controle.yaml"))
    autopsia_dir = os.getenv("ATHENA_AUTOPSIA_DIR", os.path.join(base, "autopsias"))
    pulso_path = os.getenv("ATHENA_PULSO_PATH", os.path.join(base, "pulso.json"))
    carga_episodio_path = os.getenv("ATHENA_CARGA_EPISODIO_PATH",
                                    os.path.join(base, "carga_episodio.json"))
    # ESTADO POR-CURSO EM DISCO (r15): a escada do disjuntor e os cooldowns sobrevivem ao
    # reinício. Mesmo diretório durável das outras partes (~/.athena-local por padrão).
    estado_cursos_path = os.getenv("ATHENA_ESTADO_CURSOS_PATH",
                                   os.path.join(base, "estado_cursos.json"))
    lock_dir_efetivo = lock_dir or os.path.join(base, "locks")
    batimento_intervalo = float(os.getenv("ATHENA_BATIMENTO_S", "1800"))
    # DIAGNÓSTICO por LLM da causa-raiz DESCONHECIDA: OFF por padrão (fail-closed ->
    # escalar_humano). Ligar com ATHENA_CAUSA_LLM=1 faz uma morte de causa desconhecida
    # chamar `claude -p` (subprocesso REAL, custo/tempo) para classificar. Deixado como
    # opt-in explícito para não gastar por surpresa (a maioria das mortes já é
    # determinística pelo exit_code).
    llm = causa_mod.seam_claude_p if os.getenv("ATHENA_CAUSA_LLM") == "1" else None

    # SISTEMAS GERADOS (F4-d): supervisão ao lado da captura. OPT-IN por
    # ATHENA_SISTEMAS_PATH (o registro sistemas.yaml, F4-b). Ausente => só captura
    # (default preservado, zero mudança no comportamento vivo). A fábrica é apontada
    # por ATHENA_FABRICA_PYTHON/ATHENA_FABRICA_DIR (o repo do sintetizador).
    sistemas = None
    sistema_executor = None
    causa_sistema_mod = None
    sistema_lock_dir = None
    sistema_autopsia_dir = None
    gasto_por_sistema_fn = None
    budget_modo = _BUDGET_MODO
    sistemas_path = os.getenv("ATHENA_SISTEMAS_PATH")
    if sistemas_path and os.path.exists(sistemas_path):
        from maestro import causa_sistema as causa_sistema_mod
        from maestro.adaptadores.sistema import SistemaExecutor, carregar_sistemas
        sistemas = carregar_sistemas(sistemas_path)
        fabrica_python = os.getenv("ATHENA_FABRICA_PYTHON", motor_python)
        fabrica_dir = os.getenv(
            "ATHENA_FABRICA_DIR", "/Users/guilhermerodrigues/teste/aula-sintetizador")
        sistema_lock_dir = os.path.join(base, "locks-sistemas")
        sistema_autopsia_dir = os.path.join(base, "autopsias-sistemas")
        sistema_executor = SistemaExecutor(
            sistemas, fabrica_python=fabrica_python, fabrica_dir=fabrica_dir,
            lock_dir=sistema_lock_dir)
        # GATE D5 (F4-f): a leitura de custo/dia por sistema vem da espinha real.
        # Closure zero-arg (o gate a chama sem args); dir_base=None → o padrão da
        # espinha (~/.athena-local/decisoes). budget_modo do env (default 'alerta').
        gasto_por_sistema_fn = decisoes_mod.gasto_do_dia_por_sistema

    asyncio.run(rodar(
        cursos, executor, progresso_fn, voz, intervalo_s=cfg.intervalo_s,
        max_tentativas=max_tentativas, plataformas_suportadas=plataformas_suportadas,
        controle=controle_mod, controle_path=controle_path,
        disjuntor=_DisjuntorRecozido(disjuntor_mod, max_tentativas),
        vigia=vigia_mod, causa=causa_mod, alertas=alertas, batimento=batimento_mod,
        batimento_intervalo=batimento_intervalo, pulso_path=pulso_path,
        lock_dir=lock_dir_efetivo, autopsia_dir=autopsia_dir, llm=llm,
        espinha=decisoes_mod, sistemas=sistemas, sistema_executor=sistema_executor,
        causa_sistema=causa_sistema_mod, sistema_lock_dir=sistema_lock_dir,
        sistema_autopsia_dir=sistema_autopsia_dir,
        gasto_por_sistema_fn=gasto_por_sistema_fn, budget_modo=budget_modo,
        pendencia_fn=pendencia_fn, reaper_fn=reaper_fn,
        carga_episodio_path=carga_episodio_path, zelador=zelador,
        estado_cursos_path=estado_cursos_path))


if __name__ == "__main__":  # pragma: no cover
    main()
