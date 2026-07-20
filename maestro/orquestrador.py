"""Camada 2 — ORQUESTRADOR (o CÉREBRO da Athena) com FEEDBACK LOOP.

Resolve o **Problema 3 da spec** (P3): sem feedback loop, o agente não corrige rota.
O Claude é cego na VPS — executa um comando, lê o output DAQUELE instante e reporta;
se o efeito não se sustenta (processo cai 5s depois, captura roda sem escrever no
Notion), ele nunca sabe e declara falso-sucesso. A correção é NÃO CONFIAR NO INSTANTE:
toda ação é VERIFICADA contra a FONTE DE VERDADE (Notion p/ captura, observador p/
serviços) — sucesso só quando a verdade confirma; senão, reconhece a falha, re-tenta
e re-escala.

O fluxo (o que o humano fazia na mão):
  estado OBSERVADO (Camada 1) + estado ESPERADO -> diagnosticar divergências ->
  decidir UMA ação da WHITELIST -> executar -> **verificar na fonte de verdade** ->
  confirmado: registra e segue · não confirmado: re-tenta e, esgotado, RE-ESCALA.

Tudo é COSTURA injetável e testável com dublês que modelam o mecanismo (a fonte de
verdade é um store que as ações mutam e a verificação relê) — nunca docker/HTTP/
Notion real nos testes:
  - `estado`   — snapshot do observador (Camada 1): `.servicos` e `.progresso`.
  - `acoes`    — executa a whitelist (AcoesAthena em produção; o mundo-dublê no teste).
  - `verificar(div) -> bool` — relê a FONTE DE VERDADE p/ a divergência (SÍNCRONA:
     só p/ serviços/fila, cujo efeito é imediato — restart sobe, o /health responde).
  - `voz`      — canal de escalada (Voz do maestro; FakeVoz no teste).

CAPTURA É ASSÍNCRONA (decisão de arquitetura, `curso_incompleto`): `disparar_passada`
só ENFILEIRA — a captura roda no worker residencial (minutos a horas) e o Notion só
reflete a conclusão em ciclos POSTERIORES. Logo NÃO se confirma um curso relendo o
Notion no MESMO ciclo do disparo (isso escalaria em FALSO todo ciclo, com o curso de
fato capturando). Em vez disso, a passada disparada fica "em voo" num estado que
PERSISTE entre ciclos (`voo`), e só vira:
  - CONCLUÍDA quando um ciclo POSTERIOR observar o Notion completo (a divergência
    `curso_incompleto` daquele curso deixa de existir — o observador da Camada 1 já
    leu o Notion), ou
  - ESCALADA quando, esgotada a janela de espera SEM avanço no Notion, as re-passadas
    se exaurirem (não abandona em silêncio; mas também não spamma ciclo a ciclo).
`verificar` NÃO é consultado para curso — a "verdade" do Notion chega pelo próprio
`estado.progresso` observado a cada ciclo.

WHITELIST inviolável: só estas 5 saídas de decisão existem. Qualquer divergência sem
regra vira `escalar` (fail-closed) — a Athena NUNCA chuta uma ação destrutiva."""
import time
from dataclasses import dataclass


# escalar não é uma AÇÃO executada em `acoes`; é a saída "chama o humano".
ACOES_WHITELIST = {"restart", "redeploy", "reenqueue", "disparar_passada", "escalar"}

# ações que TOCAM o mundo (têm método em `acoes`) — escalar fica de fora de propósito.
_ACOES_EXECUTAVEIS = {
    "restart": lambda ac, alvo: ac.restart(alvo),
    "redeploy": lambda ac, alvo: ac.redeploy(alvo),
    "reenqueue": lambda ac, alvo: ac.reenqueue(alvo),
    "disparar_passada": lambda ac, alvo: ac.disparar_passada(alvo),
}

# divergência -> ação da whitelist. O que não estiver aqui cai em "escalar".
_DECISAO = {
    "servico_caido": "restart",
    "servico_doente": "redeploy",
    "curso_incompleto": "disparar_passada",
    "fila_travada": "reenqueue",
}


@dataclass(frozen=True)
class Divergencia:
    """Uma diferença entre o estado OBSERVADO e o ESPERADO. Carrega tipo (a regra que
    decide a ação), alvo (serviço ou curso) e o gap honesto (esperado vs observado)."""
    tipo: str
    alvo: str
    detalhe: str = ""
    esperado: str = ""
    observado: str = ""


@dataclass(frozen=True)
class Resultado:
    """O desfecho de UMA divergência, já VERIFICADO na fonte de verdade. `confirmado`
    NÃO é "a ação respondeu OK" — é "a verdade confirmou o efeito". `escalou` marca
    que a Athena reconheceu a falha e chamou o humano (nunca declara sucesso falso).

    `em_voo` é o TERCEIRO estado, exclusivo de `curso_incompleto`: a passada foi
    ENFILEIRADA e aguarda a captura assíncrona refletir no Notion em ciclos
    posteriores — nem confirmado (a verdade ainda não provou) nem escalado (não é
    falha; é espera legítima). Um Resultado com `em_voo=True` tem sempre
    `confirmado=False` e `escalou=False`."""
    divergencia: Divergencia
    acao: str
    confirmado: bool
    tentativas: int
    escalou: bool
    detalhe: str = ""
    em_voo: bool = False


def decidir(div: Divergencia) -> str:
    """Escolhe UMA ação da whitelist p/ a divergência. Fail-closed: divergência sem
    regra -> 'escalar' (a Athena não inventa ação destrutiva sobre o desconhecido)."""
    return _DECISAO.get(div.tipo, "escalar")


def diagnosticar(estado, esperado) -> list:
    """Compara o snapshot OBSERVADO com o ESPERADO e devolve as divergências.

    `esperado` = {"servicos": {nome: {"up": True, "health": True}}, "cursos": {url: total}}.
    - serviço no esperado que não está Up            -> servico_caido
    - serviço Up mas sem /health (quando health é exigido) -> servico_doente
    - curso com done < total (total do esperado manda; senão o observado) -> curso_incompleto
      (é o gate do FALSO-PRONTO: 10/18 não é concluído)
    - alvos em `estado.fila_travada` (opcional)      -> fila_travada
    Serviço fora do esperado é ignorado (não é responsabilidade desta orquestração)."""
    divs = []
    esp_serv = (esperado or {}).get("servicos", {})
    for s in getattr(estado, "servicos", ()) or ():
        exig = esp_serv.get(s.nome)
        if exig is None:
            continue
        if not s.up:
            divs.append(Divergencia("servico_caido", s.nome,
                                    "container não está Up", "up", "down"))
        elif exig.get("health", True) and not s.health:
            divs.append(Divergencia("servico_doente", s.nome,
                                    "Up mas /health falhou", "health ok", "doente"))

    esp_cursos = (esperado or {}).get("cursos", {})
    for p in getattr(estado, "progresso", ()) or ():
        total = int(esp_cursos.get(p.curso, p.total))
        if total > 0 and p.done < total:
            divs.append(Divergencia("curso_incompleto", p.curso,
                                    f"{p.done}/{total} no Notion", str(total), str(p.done)))

    for alvo in getattr(estado, "fila_travada", ()) or ():
        divs.append(Divergencia("fila_travada", alvo,
                                "job na fila sem progresso", "avançando", "travado"))
    return divs


def resolver(div: Divergencia, acoes, verificar, voz, *, agora=None,
             max_tentativas: int = 2) -> Resultado:
    """O FEEDBACK LOOP SÍNCRONO para UMA divergência de efeito IMEDIATO
    (serviço/fila): age e relê a fonte de verdade no MESMO instante. NÃO use para
    `curso_incompleto` — a captura é assíncrona e sua confirmação é de ciclos
    posteriores; o `orquestrar` a roteia para `_passada_em_voo`, não para cá.

    1. decide a ação (whitelist).
    2. se 'escalar' (ou fora da whitelist executável): NÃO age — escala e retorna.
    3. senão, até `max_tentativas`: executa a ação e **verifica na fonte de verdade**.
       - verdade confirma -> registra sucesso e retorna (confirmado=True).
       - não confirma (ou a ação levanta) -> re-tenta.
    4. esgotadas as tentativas SEM confirmação -> reconhece a falha, ESCALA e retorna
       confirmado=False (JAMAIS declara sucesso pelo output do instante)."""
    acao = decidir(div)
    executor = _ACOES_EXECUTAVEIS.get(acao)
    if acao == "escalar" or executor is None:
        # decisão real / fail-closed: não toca no mundo, chama o humano.
        _escalar(voz, div, f"[{div.alvo}] divergência '{div.tipo}' sem ação automática "
                           f"segura — precisa de decisão. {div.detalhe}".strip())
        return Resultado(div, "escalar", confirmado=False, tentativas=0,
                         escalou=True, detalhe="sem ação automática — escalado")

    tentativas = 0
    for _ in range(max(1, int(max_tentativas))):
        tentativas += 1
        try:
            executor(acoes, div.alvo)               # output do INSTANTE é IGNORADO
        except Exception as e:                       # ação falhou de cara -> re-tenta
            ultimo_erro = f"ação levantou: {e}"
            continue
        try:
            confirmado = verificar(div)              # a VERDADE, não o instante
        except Exception as e:
            # reler a fonte de verdade ESTOUROU (Notion fora, docker exec sem
            # container, socket off). Isso NÃO é confirmação. Fail-closed: trata como
            # não-confirmado, re-tenta e, esgotado, escala — a exceção JAMAIS vaza de
            # `resolver` (vazar abortaria a orquestração e deixaria a ação já
            # executada sem veredito).
            ultimo_erro = f"verificação da fonte de verdade levantou: {e}"
            continue
        if confirmado:                               # a VERDADE, não o instante
            return Resultado(div, acao, confirmado=True, tentativas=tentativas,
                             escalou=False, detalhe="confirmado na fonte de verdade")
        ultimo_erro = "efeito não confirmado na fonte de verdade"

    # nunca confirmou: reconhece a falha honestamente e re-escala.
    _escalar(voz, div, f"[{div.alvo}] ação '{acao}' NÃO confirmou na fonte de verdade "
                       f"após {tentativas} tentativa(s) ({ultimo_erro}) — reescalando.")
    return Resultado(div, acao, confirmado=False, tentativas=tentativas,
                     escalou=True, detalhe=ultimo_erro)


def orquestrar(estado, esperado, acoes, verificar, voz, *, agora=None,
               max_tentativas: int = 2, voo=None, espera_s: float = 1800.0) -> list:
    """Roda o cérebro sobre TODO o snapshot: diagnostica as divergências e resolve
    cada uma. Serviços/fila fecham o loop SÍNCRONO (age -> verifica na fonte de
    verdade agora). Curso é ASSÍNCRONO: enfileira a passada e confirma/escala em
    ciclos POSTERIORES via `voo`.

    NOTA (decisão do dono): o DISPARO DE CAPTURA DE CURSO NÃO passa mais por aqui — o
    dono é `orquestrar_captura`, que DELEGA a passada ao `captura.coordenar` (todas as
    travas: sessão, anti-dup por completude, auto-ingest, Sintetizador, gate I-1). Esta
    função permanece como o loop de feedback SÍNCRONO genérico (serviços/fila) e o
    modelo de disparo-cru assíncrono (`disparar_passada` via AcoesAthena), ainda
    exercitado em teste; o ramo de curso aqui não é o caminho de produção.

    `voo` é o estado que PERSISTE entre ciclos (o chamador — o loop — o cria uma vez
    e o passa DE VOLTA a cada ciclo). Mapeia curso -> passada em voo. Sem ele (None),
    um `voo` efêmero é criado: o disparo do ciclo atual não escala em falso, mas a
    CONFIRMAÇÃO (que é de ciclos posteriores) só acontece se o MESMO dict for
    reinjetado nos ciclos seguintes.

    `espera_s` é a janela sem-avanço no Notion antes de re-empurrar a passada; só
    depois de esgotar as `max_tentativas` sem avanço é que se ESCALA (não abandona).

    Devolve a lista de Resultados. O que confirma, registra; o que ainda captura,
    fica `em_voo`; o que falha de verdade, escala — sem nunca declarar um done que a
    fonte de verdade não provou, nem escalar um curso que só está capturando."""
    if voo is None:
        voo = {}
    if agora is None:
        agora = time.time()

    divs = diagnosticar(estado, esperado)
    incompletos = {d.alvo: d for d in divs if d.tipo == "curso_incompleto"}
    observados = _cursos_observados(estado, esperado)
    resultados = []

    # (A) CONFIRMAÇÃO POSTERIOR: passadas em voo cujo curso o Notion agora mostra
    # COMPLETO. "Completo" = foi OBSERVADO neste ciclo (está em `observados`) e NÃO
    # está mais entre os incompletos => done>=total no Notion. É AQUI, num ciclo
    # posterior ao disparo, que uma passada vira 'concluída' — nunca no mesmo ciclo.
    # Curso em voo NÃO observado neste ciclo segue esperando (não confirma às cegas).
    for alvo in list(voo):
        if alvo in incompletos or alvo not in observados:
            continue
        info = voo.pop(alvo)
        resultados.append(Resultado(
            info["div"], "disparar_passada", confirmado=True,
            tentativas=info["tentativas"], escalou=False,
            detalhe="conclusão CONFIRMADA no Notion em ciclo posterior"))

    # (B) SERVIÇOS/FILA: loop síncrono (efeito imediato, verifica agora).
    for div in divs:
        if div.tipo == "curso_incompleto":
            continue
        resultados.append(resolver(div, acoes, verificar, voz, agora=agora,
                                   max_tentativas=max_tentativas))

    # (C) CURSO ainda incompleto: enfileira (1º ciclo) ou aguarda/re-empurra/escala
    # (ciclos posteriores) — assíncrono, sem reler o Notion sincronamente.
    for alvo, div in incompletos.items():
        resultados.append(_passada_em_voo(div, acoes, voz, voo, agora,
                                          max_tentativas, espera_s))
    return resultados


def _cursos_observados(estado, esperado) -> dict:
    """Curso -> (done, total) que o observador (Camada 1) leu do Notion NESTE ciclo,
    com o total do esperado sobrepondo o observado (mesma regra do diagnosticar)."""
    esp = (esperado or {}).get("cursos", {})
    out = {}
    for p in getattr(estado, "progresso", ()) or ():
        out[p.curso] = (int(p.done), int(esp.get(p.curso, p.total)))
    return out


def _obs_int(valor) -> int:
    try:
        return int(valor)
    except (TypeError, ValueError):
        return 0


def _passada_em_voo(div, acoes, voz, voo, agora, max_tentativas, espera_s) -> Resultado:
    """O ciclo de vida ASSÍNCRONO de UMA passada de curso, através de `voo`.

    1º ciclo (não está em voo): ENFILEIRA a passada e a marca 'em voo'. NÃO relê o
      Notion (a captura é assíncrona) e por isso NÃO escala — escalar aqui seria o
      falso-negativo que este redesenho existe p/ matar. Se o ENFILEIRAR em si falha,
      escala honesto e NÃO marca em voo (o próximo ciclo re-tenta).
    Ciclos posteriores, ainda incompleto:
      - avançou no Notion (done subiu) -> segue em voo, reinicia a janela, sem
        re-disparar nem escalar (a captura está progredindo).
      - sem avanço, dentro da janela `espera_s` -> AGUARDA (não spamma, não escala).
      - sem avanço, janela esgotada e ainda há tentativas -> RE-empurra UMA passada.
      - sem avanço, tentativas esgotadas -> reconhece e ESCALA (não abandona)."""
    alvo = div.alvo
    done_atual = _obs_int(div.observado)
    info = voo.get(alvo)

    if info is None:
        try:
            acoes.disparar_passada(alvo)          # só ENFILEIRA; retorno ignorado
        except Exception as e:
            _escalar(voz, div, f"[{alvo}] falha ao ENFILEIRAR a passada ({e}) — "
                               f"não marquei em voo; re-tento no próximo ciclo.")
            return Resultado(div, "disparar_passada", confirmado=False, tentativas=1,
                             escalou=True, detalhe=f"falha ao enfileirar: {e}")
        voo[alvo] = {"div": div, "disparada_em": agora, "tentativas": 1,
                     "ultimo_done": done_atual}
        return Resultado(div, "disparar_passada", confirmado=False, tentativas=1,
                         escalou=False, em_voo=True,
                         detalhe="passada ENFILEIRADA — aguardando o Notion (ciclo posterior)")

    if done_atual > info["ultimo_done"]:
        info["ultimo_done"] = done_atual
        info["disparada_em"] = agora            # progrediu: reinicia a janela
        return Resultado(div, "disparar_passada", confirmado=False,
                         tentativas=info["tentativas"], escalou=False, em_voo=True,
                         detalhe="avançando no Notion — aguardando conclusão")

    if agora - info["disparada_em"] < espera_s:
        return Resultado(div, "disparar_passada", confirmado=False,
                         tentativas=info["tentativas"], escalou=False, em_voo=True,
                         detalhe="aguardando (dentro da janela de espera)")

    if info["tentativas"] < max_tentativas:
        try:
            acoes.disparar_passada(alvo)
        except Exception as e:
            _escalar(voz, div, f"[{alvo}] passada parada e o RE-disparo falhou ({e}).")
            return Resultado(div, "disparar_passada", confirmado=False,
                             tentativas=info["tentativas"], escalou=True,
                             detalhe=f"re-disparo falhou: {e}")
        info["tentativas"] += 1
        info["disparada_em"] = agora
        return Resultado(div, "disparar_passada", confirmado=False,
                         tentativas=info["tentativas"], escalou=False, em_voo=True,
                         detalhe="passada parada — RE-enfileirada")

    voo.pop(alvo, None)
    _escalar(voz, div, f"[{alvo}] passada NÃO avançou no Notion após "
                       f"{info['tentativas']} passada(s) e a janela de espera — "
                       f"reescalando. {div.detalhe}".strip())
    return Resultado(div, "disparar_passada", confirmado=False,
                     tentativas=info["tentativas"], escalou=True,
                     detalhe="passada não avançou no Notion — escalado")


def _escalar(voz, div, pedido: str) -> None:
    if voz is not None:
        voz.escalar(div, pedido)


# =====================================================================
# OWNER do disparo de captura de curso (a decisão do dono): o ORQUESTRADOR
# (Camada 2) é o DONO do disparo — mas NÃO reimplementa nenhuma das travas do
# coordenar. Ele DELEGA cada passada ao EXECUTOR (o `captura.coordenar`), que
# carrega TODAS as travas testadas, e ENVOLVE-a com o que só a Camada 2 tem:
# vigília de STALL pela FONTE DE VERDADE (Notion) e escalada honesta.
# =====================================================================
@dataclass(frozen=True)
class ResultadoCurso:
    """Desfecho de UMA passada de curso pela Camada 2 (o owner). `acao` é o que o
    EXECUTOR (coordenar) reportou neste ciclo (Acao|None) — todas as travas a-d,f já
    correram DENTRO dele. `concluido` só é True quando o Notion PROVA completude
    (no_notion >= total, total>0): completude-por-Notion, nunca por flag nem por
    presença. `stall_escalado` marca que o owner reconheceu ESTAGNAÇÃO (o Notion parou
    de avançar) e escalou — o valor que a Camada 2 agrega sobre o coordenar, que fica
    quieto para sempre. `progrediu` = o Notion avançou (a captura está andando)."""
    curso: str
    acao: object = None
    concluido: bool = False
    stall_escalado: bool = False
    progrediu: bool = False


def orquestrar_captura(cursos, passada_fn, notion_fn, voz, voo, *, agora=None,
                       espera_s=1800.0) -> list:
    """O ORQUESTRADOR como DONO do disparo de captura de curso. Para CADA curso, roda
    UMA passada pelo EXECUTOR `passada_fn(curso)` — o `captura.coordenar`, que É o dono
    testado das travas: (a) sessão VIVA (nunca loga sozinho), (b) anti-dup por
    COMPLETUDE (no_notion>=total, não por presença), (c) auto-ingest confirmado, (d)
    hook do Sintetizador/Cap C pós-captura, (f) completude-por-Notion (gate I-1). O
    owner NÃO reimplementa nenhuma delas — só as INVOCA através do coordenar (reuso, não
    duplicação). O gate de plataforma-nova (e) é aplicado ANTES desta chamada (no loop),
    de modo que um curso sem adaptador nunca chega aqui.

    O que o owner ADICIONA (o motivo de a Camada 2 ser o dono, e não o coordenar
    sozinho): a VIGÍLIA DE STALL pela fonte de verdade. O coordenar, uma vez capturando,
    fica quieto para sempre — nunca reconhece que a captura EMPACOU. O owner lê o
    numerador REAL no Notion a cada ciclo (`notion_fn`), guarda-o cross-ciclo em `voo` e,
    se um curso PARA de avançar por `espera_s`, ESCALA uma vez (latch anti-spam) — a
    Athena não abandona em silêncio, mas também não martela.

    INVIOLÁVEIS honrados aqui:
      - ANTI-BAN: o owner NUNCA captura nem enfileira; quem enfileira é o coordenar
        (idempotente por URL). Chamar a passada a cada ciclo NÃO re-enfileira um curso
        já ativo (a fase do coordenar persiste em `estado`). Uma passada por ciclo.
      - COMPLETUDE-POR-NOTION / anti-falso-pronto: `concluido` exige no_notion>=total
        com total>0. total<=0 (enumeração transitoriamente indisponível — o worker
        ainda não resolveu o course_id) NUNCA prova conclusão: o owner NÃO confirma e
        NÃO julga stall (sem denominador confiável) — apenas AGUARDA. Isso mata o
        falso-positivo de uma enum=0 transitória virar "curso concluído".

    `passada_fn(curso) -> Acao|None`: uma passada do coordenar (todas as travas).
    `notion_fn(curso) -> (no_notion|None, total)`: contagem REAL no Notion NESTE ciclo
      (numerador) + total esperado (denominador). no_notion None => não deu para ler
      agora — o owner não julga o curso às cegas (nem confirma, nem escala stall).
    `voo`: store cross-ciclo por curso {desde, ultimo, avisado}. Criado UMA vez pelo
      chamador e REINJETADO a cada ciclo — sem isso a vigília de stall (cross-ciclo)
      nunca fecha. Um curso que conclui ou some é retirado do `voo`.
    """
    if voo is None:
        voo = {}
    if agora is None:
        agora = time.time()

    resultados = []
    for curso in cursos:
        # ---- EXECUTOR: a passada do coordenar (travas a-d, f correm AQUI DENTRO) ----
        # O coordenar dirige a própria `voz` (pede reseed / avisa / escala honesto) e
        # é idempotente por ciclo (a fase persiste em `estado`). Se a passada em si
        # ESTOURA (bug/infra fora do tratamento do coordenar), o owner NÃO pode
        # derrubar os demais cursos: reconhece, escala e segue (fail-closed).
        try:
            acao = passada_fn(curso)
        except Exception as e:
            _escalar(voz, Divergencia("captura_passada_estourou", curso, str(e)[:160]),
                     f"[{curso}] a passada de captura ESTOUROU inesperadamente "
                     f"({str(e)[:160]}) — segue os demais; re-tento no próximo ciclo.")
            resultados.append(ResultadoCurso(curso, None, stall_escalado=True))
            continue

        # ---- VIGÍLIA DE STALL pela FONTE DE VERDADE (Notion) — só a Camada 2 ----
        try:
            no_notion, total = notion_fn(curso)
        except Exception:
            no_notion, total = None, 0
        if no_notion is None:
            resultados.append(ResultadoCurso(curso, acao))   # não leu o Notion: não julga
            continue

        no_notion, total = int(no_notion), int(total)
        # completude-por-Notion: só conclui quando o Notion PROVA (>=total, total>0).
        if total > 0 and no_notion >= total:
            voo.pop(curso, None)                              # concluído: para de vigiar
            resultados.append(ResultadoCurso(curso, acao, concluido=True))
            continue
        if total <= 0:
            # enum transitória/desconhecida: SEM denominador confiável não se confirma
            # NEM se julga stall (o falso-pronto por enum=0 morre aqui). Só aguarda.
            resultados.append(ResultadoCurso(curso, acao))
            continue

        info = voo.get(curso)
        if info is None:
            voo[curso] = {"desde": agora, "ultimo": no_notion, "avisado": False}
            resultados.append(ResultadoCurso(curso, acao))
            continue
        if no_notion > info["ultimo"]:                        # avançou: reinicia a janela
            info["ultimo"] = no_notion
            info["desde"] = agora
            info["avisado"] = False
            resultados.append(ResultadoCurso(curso, acao, progrediu=True))
            continue
        # sem avanço: dentro da janela -> aguarda; esgotada -> escala UMA vez (latch).
        if agora - info["desde"] >= espera_s and not info["avisado"]:
            info["avisado"] = True
            espera = int(agora - info["desde"])
            _escalar(voz, Divergencia("captura_estagnada", curso,
                                      f"{no_notion}/{total} no Notion sem avanço"),
                     f"[{curso}] captura ESTAGNADA: {no_notion}/{total} no Notion sem "
                     f"avanço há {espera}s — a passada segue enfileirada mas o worker "
                     f"não progride; precisa de olho humano (não abandono em silêncio).")
            resultados.append(ResultadoCurso(curso, acao, stall_escalado=True))
            continue
        resultados.append(ResultadoCurso(curso, acao))        # aguardando (dentro da janela)
    return resultados


class AcoesAthena:
    """Concretiza a WHITELIST sobre as costuras REAIS já existentes — sem abrir nada
    novo: `acesso` (docker restart / Easypanel redeploy, de acesso.py) e o `executor`
    de captura (FilaExecutor.disparar, que ENFILEIRA — VPS-safe). Cada método É a
    ação; o EFEITO é verificado POR FORA (o feedback loop), nunca pelo retorno daqui.

    Sem `executor`, disparar_passada/reenqueue LEVANTAM — não engolem o disparo em
    silêncio (silêncio viraria falso-pronto no loop)."""

    def __init__(self, acesso, projeto, *, executor=None):
        self._acesso = acesso
        self._projeto = projeto
        self._executor = executor

    def restart(self, alvo):
        self._acesso.restart(alvo)

    def redeploy(self, alvo):
        self._acesso.redeploy(alvo, self._projeto.projeto_easypanel)

    def _disparar(self, alvo):
        if self._executor is None:
            raise RuntimeError(
                f"AcoesAthena sem executor: não há como disparar captura de {alvo}")
        return self._executor.disparar(alvo)

    def disparar_passada(self, alvo):
        return self._disparar(alvo)

    def reenqueue(self, alvo):
        return self._disparar(alvo)
