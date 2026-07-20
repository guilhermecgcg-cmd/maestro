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
  - `verificar(div) -> bool` — relê a FONTE DE VERDADE p/ a divergência.
  - `voz`      — canal de escalada (Voz do maestro; FakeVoz no teste).

WHITELIST inviolável: só estas 5 saídas de decisão existem. Qualquer divergência sem
regra vira `escalar` (fail-closed) — a Athena NUNCA chuta uma ação destrutiva."""
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
    que a Athena reconheceu a falha e chamou o humano (nunca declara sucesso falso)."""
    divergencia: Divergencia
    acao: str
    confirmado: bool
    tentativas: int
    escalou: bool
    detalhe: str = ""


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
    """O FEEDBACK LOOP para UMA divergência.

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
        if verificar(div):                           # a VERDADE, não o instante
            return Resultado(div, acao, confirmado=True, tentativas=tentativas,
                             escalou=False, detalhe="confirmado na fonte de verdade")
        ultimo_erro = "efeito não confirmado na fonte de verdade"

    # nunca confirmou: reconhece a falha honestamente e re-escala.
    _escalar(voz, div, f"[{div.alvo}] ação '{acao}' NÃO confirmou na fonte de verdade "
                       f"após {tentativas} tentativa(s) ({ultimo_erro}) — reescalando.")
    return Resultado(div, acao, confirmado=False, tentativas=tentativas,
                     escalou=True, detalhe=ultimo_erro)


def orquestrar(estado, esperado, acoes, verificar, voz, *, agora=None,
               max_tentativas: int = 2) -> list:
    """Roda o cérebro sobre TODO o snapshot: diagnostica as divergências e resolve
    cada uma pelo feedback loop. Devolve a lista de Resultados (vazia = tudo no
    esperado, nada a fazer). O que confirma, registra; o que não confirma, escala —
    sem nunca declarar um done que a fonte de verdade não provou."""
    resultados = []
    for div in diagnosticar(estado, esperado):
        resultados.append(resolver(div, acoes, verificar, voz, agora=agora,
                                   max_tentativas=max_tentativas))
    return resultados


def _escalar(voz, div, pedido: str) -> None:
    if voz is not None:
        voz.escalar(div, pedido)


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
