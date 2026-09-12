"""Disjuntor com RECOZIMENTO — o coração do never-stop (C2).

Substitui o TETO PERMANENTE do disjuntor antigo (athena_local.py:113-121), que
ao bater `tentativas >= max_tentativas` (3) parava de disparar PARA SEMPRE até um
humano intervir. Num coordenador 24/7 sem-parada isso é o defeito estrutural: uma
falha transitória (plataforma fora do ar por 5min, sessão que expirou, 429 da
API) condenava o curso à parada eterna, e o "never-stop" deixava de valer.

Aqui o disjuntor é RE-ARMÁVEL. Ao acumular `limiar` (3) falhas consecutivas ele
NÃO trava para sempre: entra numa janela de backoff exponencial e, quando essa
janela vence, RE-ARMA sozinho e libera UMA nova tentativa. Se ela falhar de novo,
a próxima janela é maior; se der certo, `registrar_sucesso` zera tudo e a escada
recomeça do degrau zero. A escada:

    limiar (3ª falha)  -> 600s   (10 min)
    +1 falha (4ª)      -> 3600s  (1 h)
    +2 falhas (5ª)     -> 21600s (6 h)
    +3 falhas (6ª+)    -> 86400s (24 h, TETO — satura aqui, não cresce mais)

A ÚNICA forma de parada permanente é uma causa IRREDUTÍVEL: quando o
classificador externo (causa.classificar, fiado por P1) marca `st['irredutivel']`
— curso deletado, acesso revogado, adaptador inexistente — martelar é inútil e o
disjuntor fica aberto de vez. Enquanto a flag não existir/for falsa, TODA parada
é temporária por construção.

Contrato de PUREZA (para o loop-dono poder recomputar o estado após qualquer
morte de processo e para os testes serem determinísticos):
  * O relógio entra como parâmetro `agora` (float, epoch em segundos). O módulo
    NUNCA chama time.time() por baixo.
  * O estado vive no dict por-curso `st` já existente (o mesmo que carrega
    "fase", "tentativas"...). O disjuntor só toca chaves com prefixo `disj_`
    (e LÊ `irredutivel`, escrita por quem classifica).
  * PERSISTÊNCIA (r15): as chaves `disj_falhas`/`disj_bloqueado_ate` são gravadas
    em disco ao fim de cada ciclo e restauradas no boot pelo loop-dono
    (`athena_local._gravar_estado_cursos` / `_carregar_estado_cursos`), de modo que
    a escada ATRAVESSA um reinício do daemon. Até a r15 isso era só uma promessa
    do comentário: o `estado` nascia `{}` a cada `rodar()` e QUALQUER reinício
    (vigia externo, launchd, reboot) zerava um cooldown de até 24 h — o curso
    voltava a ser martelado, que é exatamente o que a escada existe para evitar.
    O disjuntor em si continua PURO: quem faz I/O é o loop-dono.

P1 fia estas três funções no `_passada_local_fn`:
  * `pode_tentar(st, agora)` no lugar do antigo `tentativas >= max_tentativas`;
  * `registrar_falha(st, agora)` quando uma passada falha de verdade;
  * `registrar_sucesso(st)` quando o curso progride/conclui.
"""

# A escada de backoff, em segundos. O último valor é o TETO: falhas além do fim
# da lista continuam usando 86400s. Manter como tupla (imutável, fonte única).
ESCADA_S = (600, 3600, 21600, 86400)  # 10 min, 1 h, 6 h, 24 h (teto)

# Nº de falhas consecutivas que ARMA a primeira janela. Igual ao `max_tentativas`
# antigo (3): as duas primeiras falhas são "de graça" (transitório comum), a
# terceira aciona o recozimento. Injetável para calibração por plataforma — e, desde
# a r15, de fato CONFIGURÁVEL em produção pela env ATHENA_MAX_TENTATIVAS, que o
# `athena_local._DisjuntorRecozido` fia em todas as chamadas. (Antes da r15 a env
# existia, era lida e não valia NADA: o limiar real era sempre este 3.)
LIMIAR_PADRAO = 3


def _janela_s(falhas: int, limiar: int) -> float:
    """Duração da janela de bloqueio para `falhas` consecutivas.

    Degrau = falhas ALÉM do limiar (a 3ª falha => degrau 0 => 600s). Satura no
    último valor da ESCADA (o teto). Só faz sentido chamar com falhas >= limiar.
    """
    degrau = falhas - limiar
    if degrau < 0:
        return 0.0
    if degrau >= len(ESCADA_S):
        degrau = len(ESCADA_S) - 1
    return float(ESCADA_S[degrau])


def pode_tentar(st: dict, agora: float, *, limiar: int = LIMIAR_PADRAO) -> bool:
    """True se o disjuntor deixa disparar agora; False se está bloqueado.

    Precedência:
      1. `irredutivel` verdadeiro  -> False SEMPRE (parada permanente).
      2. falhas < limiar           -> True (ainda no crédito de tentativas livres).
      3. falhas >= limiar          -> True somente se a janela de backoff JÁ venceu
                                       (agora >= disj_bloqueado_ate). Ao vencer, é
                                       o próprio re-arm: libera UMA nova tentativa.

    Puro no relógio: o veredito depende só de `st` e de `agora`. Não muta `st`.
    """
    if st.get("irredutivel"):
        return False
    if st.get("disj_falhas", 0) < limiar:
        return True
    return agora >= st.get("disj_bloqueado_ate", 0.0)


def registrar_falha(st: dict, agora: float, *, limiar: int = LIMIAR_PADRAO) -> None:
    """Contabiliza UMA falha consecutiva e (re)arma a janela de bloqueio.

    Enquanto `falhas < limiar` só conta. No limiar em diante, arma
    `disj_bloqueado_ate = agora + janela(falhas)` — janela que cresce a cada
    falha adicional (recozimento). NÃO marca `irredutivel`: essa é decisão do
    classificador (causa.classificar), não do disjuntor.
    """
    st["disj_falhas"] = st.get("disj_falhas", 0) + 1
    if st["disj_falhas"] >= limiar:
        st["disj_bloqueado_ate"] = agora + _janela_s(st["disj_falhas"], limiar)


def registrar_sucesso(st: dict) -> None:
    """Zera o disjuntor: falhas consecutivas viram 0 e a janela é descartada.

    A escada volta ao degrau zero — a próxima batelada de falhas recomeça em
    600s, não no último teto atingido. NÃO toca `irredutivel`: uma causa raiz
    irredutível só é desmarcada por quem a classificou (ou por um humano); um
    sinal de sucesso solto não deve "descondenar" um curso deletado/revogado.
    """
    st.pop("disj_falhas", None)
    st.pop("disj_bloqueado_ate", None)
