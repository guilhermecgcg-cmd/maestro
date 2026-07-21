"""Batimento (heartbeat) da Athena: de tempos em tempos, um sinal de VIDA no
Telegram — "viva: N capturas ativas, X/Y no Notion, uptime ..." — pela Voz já
fiada. É a ÚNICA mensagem proativa-e-periódica do sistema: os ALERTAS nascem de
PROBLEMAS (Sentinela → Voz), o BATIMENTO nasce do RELÓGIO. Serve de "dead-man's
switch" invertido: enquanto o batimento chega, o dono sabe que o daemon está
vivo; quando PARA de chegar, o silêncio é o próprio alarme.

REGRAS INVIOLÁVEIS (travadas por teste):
  1. best-effort — talvez_bater() NUNCA levanta e NUNCA derruba o loop. Se o
     resumo ou o envio falharem, engole e segue (o Never-Stop não pode morrer
     por causa de um heartbeat).
  2. MUDO é silêncio de verdade — quando TELEGRAM_BOT_TOKEN=MUTED (ou ausente,
     fail-safe), NÃO há envio NENHUM. Nada de POST para a Bot API com um token
     inválido só para falhar e ser engolido: se está mudo, nem tenta.
  3. um batimento que caiu (mudo/falha) AINDA avança o relógio — assim não vira
     tempestade de retry a cada ciclo de 120s; o PRÓXIMO intervalo tenta de novo.
"""
import logging
import os

_MUDO = "MUTED"

log = logging.getLogger("athena.batimento")


def _esta_mudo() -> bool:
    """Fail-safe: sem token REAL, considera-se mudo (não sai POST nenhum).
    Vazio/espaço conta como mudo tanto quanto o literal MUTED — assim o módulo
    é seguro por si só, sem depender do `:-MUTED` do launch.sh converter vazio."""
    return os.getenv("TELEGRAM_BOT_TOKEN", _MUDO).strip() in ("", _MUDO)


def _emitir(voz, texto: str) -> None:
    """Emite pela Voz. Prefere um método público `bater(texto)` se a Voz o
    expuser; senão cai no primitivo `_enviar(texto)` da Voz real (maestro/voz.py),
    que já manda para todos os chats. O batimento não é uma ação (🔧) nem uma
    escalada (⚠️): é uma linha limpa de 'viva:', então usa o envio cru."""
    enviar = getattr(voz, "bater", None) or getattr(voz, "_enviar")
    enviar(texto)


def talvez_bater(voz, resumo_fn, agora, ultimo, intervalo=1800):
    """Talvez emita um batimento. Devolve o novo `ultimo` (timestamp do último
    batimento). Só bate quando já se passou `intervalo` desde `ultimo`.

    voz       -- a Voz já fiada (expõe `bater` ou `_enviar`).
    resumo_fn -- callable sem args que devolve o corpo do resumo, ex.:
                 "2 capturas ativas, 487/500 no Notion, uptime 5h". Chamado só
                 quando é hora de bater (para não pagar a contagem à toa).
    agora     -- timestamp atual (segundos).
    ultimo    -- timestamp do último batimento (0.0 no boot => bate no 1º ciclo).
    intervalo -- segundos entre batimentos (default 1800s = 30min).
    """
    if agora - ultimo < intervalo:
        return ultimo                      # ainda não é hora; relógio intacto

    # É hora. Avança o relógio ANTES de qualquer I/O: um envio que falhe (ou um
    # bot mudo) NÃO pode fazer o loop marteler a cada ciclo — o próximo intervalo
    # tenta de novo (regra 3).
    novo_ultimo = agora

    # Monta o corpo ANTES de decidir o canal. O resumo é best-effort: se a
    # contagem falhar, o batimento cai em silêncio (regra 1) sem log nem envio.
    try:
        texto = f"viva: {resumo_fn()}"
    except Exception:
        return novo_ultimo                 # best-effort: resumo falhou (regra 1)

    # OBSERVABILIDADE fail-safe: registra SEMPRE a linha que iria pro Telegram —
    # inclusive quando MUDO. É o que prova, no log local, que o batimento emitiu
    # (o dead-man's switch invertido do daemon) mesmo sem token. Não é um POST:
    # a regra 2 (mudo = nenhum envio à Bot API) segue intacta logo abaixo.
    log.info("[batimento] %s", texto)

    if _esta_mudo():
        return novo_ultimo                 # mudo => log sim, POST não (regra 2)

    try:
        _emitir(voz, texto)
    except Exception:
        pass                               # best-effort: nunca levanta (regra 1)
    return novo_ultimo
