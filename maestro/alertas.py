"""Alertas de supervisão da Athena via Telegram. A Athena chama estas funções em
eventos de supervisão — captura morreu, sessão expirou, curso concluído — e o dono
recebe no Telegram. Reusa o TelegramClient da ponte (retry/429 já embutido). Só
SAÍDA: nenhuma porta de entrada nova (INVIOLÁVEL 3).

Fail-safe é a regra central deste módulo: sem token/cliente, o alerta vira log e
NUNCA levanta. O canal que existe para avisar que o supervisor caiu jamais pode ser
ele próprio quem derruba o supervisor — por isso todo envio é best-effort e toda
exceção do Telegram é engolida (a ponte já tem retry para o hiccup transitório).

GATE DE ESSENCIALIDADE (fix do ruído 22/07): o Telegram só recebe o que exige AÇÃO
HUMANA ou é incidente real; todo o resto (flap que re-tenta sozinho, bench exit-5,
morte transitória auto-tratada) vira SÓ-LOG — o arquivo de log continua registrando
TUDO, só o CANAL fica limpo. O call-site declara `essencial=True` no que pede humano;
`ATHENA_ALERTA_NIVEL=tudo` restaura o comportamento antigo (debug). Regra fail-safe
de classificação: pede ação humana => essencial; auto-tratado => só-log.

INVARIANTE (anti-ban): `sessao_expirada` (reseed) NUNCA passa pelo gate nem pelo
dedup — é o único jeito de o dono saber que precisa refazer o login.

DEDUP: um alerta essencial com `chave` (ex.: ("humano", curso)) não repete dentro da
janela `ATHENA_ALERTA_DEDUP_S` (default 1h) — 2 mortes iguais do mesmo curso viram
1 ping + log. Estado em memória: reinício do daemon zera (na dúvida, re-alerta)."""
import logging
import os
import time

from maestro.telegram_api import TelegramClient

log = logging.getLogger("athena.alertas")

_NIVEIS = ("essencial", "tudo")
_DEDUP_JANELA_PADRAO_S = 3600.0


class Alertas:
    """Emite alertas de supervisão. `tg=None` => modo só-log (fail-safe), para a
    Athena poder chamar alertas mesmo sem canal Telegram configurado.

    `nivel`: 'essencial' (default — só o que exige ação humana pinga o Telegram)
    ou 'tudo' (comportamento antigo: tudo pinga). `dedup_janela_s` e `relogio`
    são injetáveis para teste; defaults vêm do ambiente/`time.time`."""

    def __init__(self, tg, chat_ids, nivel=None, dedup_janela_s=None,
                 relogio=time.time):
        self._tg = tg
        self._chats = list(chat_ids)
        nivel = (nivel or os.getenv("ATHENA_ALERTA_NIVEL", "essencial"))
        nivel = str(nivel).strip().lower()
        # fail-safe: valor desconhecido cai em 'essencial' (o default que corta ruído).
        self._nivel = nivel if nivel in _NIVEIS else "essencial"
        if dedup_janela_s is None:
            try:
                dedup_janela_s = float(
                    os.getenv("ATHENA_ALERTA_DEDUP_S", str(_DEDUP_JANELA_PADRAO_S)))
            except (TypeError, ValueError):
                dedup_janela_s = _DEDUP_JANELA_PADRAO_S
        self._dedup_janela_s = float(dedup_janela_s)
        self._relogio = relogio
        self._ultimo_envio = {}          # chave -> ts do último envio Telegram

    def _enviar(self, texto: str) -> bool:
        """True = o texto CHEGOU a pelo menos um chat (só isso conta pro dedup)."""
        if self._tg is None or not self._chats:
            log.warning("alerta sem canal Telegram (só-log): %s", texto)
            return False
        chegou = False
        for chat in self._chats:
            try:
                self._tg.send_message(chat, texto)
                chegou = True
            except Exception:
                # best-effort: o alerta nunca pode derrubar quem o chamou.
                log.exception("falha ao enviar alerta para chat %s", chat)
        return chegou

    def _suprimido_nao_essencial(self, essencial: bool, texto: str) -> bool:
        """True = fica SÓ no log (auto-tratado; nenhuma ação do dono é necessária)."""
        if essencial or self._nivel == "tudo":
            return False
        log.warning("alerta NÃO-essencial (só-log, sem Telegram): %s", texto)
        return True

    def _dedup_repetido(self, chave, texto: str) -> bool:
        """True = mesma chave já ENVIOU (com sucesso) dentro da janela — vira só-log.
        Fail-safe total: sem chave, em nivel='tudo' (comportamento antigo integral),
        com relógio quebrado ou chave não-hashable => ENVIA (o dedup jamais engole
        por bug, e jamais levanta pro chamador)."""
        if chave is None or self._dedup_janela_s <= 0 or self._nivel == "tudo":
            return False
        try:
            agora = float(self._relogio())
            ts = self._ultimo_envio.get(chave)
        except Exception:
            return False
        if ts is not None and (agora - ts) < self._dedup_janela_s:
            log.warning("alerta dedupado (mesma chave na janela, só-log): %s", texto)
            return True
        return False

    def _marcar_enviado(self, chave) -> None:
        """Registra o envio BEM-SUCEDIDO da chave (review IMPORTANTE-1: um envio
        que FALHOU não pode armar o dedup — a próxima morte re-tenta o Telegram).
        Nunca levanta (chave não-hashable/relógio quebrado => só não marca)."""
        if chave is None:
            return
        try:
            self._ultimo_envio[chave] = float(self._relogio())
        except Exception:
            pass

    def captura_morreu(self, plataforma: str, motivo: str, *,
                       essencial: bool = False, chave=None) -> None:
        """Morte de captura/sistema. `essencial=True` SÓ quando o evento exige ação
        humana (token/desconhecida/escalada) — flap/bench/transitório auto-tratados
        ficam no default só-log. `chave` (hashable, ex.: ("humano", curso)) dedupa
        o MESMO alerta do MESMO curso dentro da janela."""
        texto = (f"🔴 Captura {plataforma} MORREU — {motivo}. "
                 f"Fila sem supervisão; retomar exige olhar humano.")
        if self._suprimido_nao_essencial(essencial, texto):
            return
        chave_dedup = ("captura_morreu", chave) if chave is not None else None
        if self._dedup_repetido(chave_dedup, texto):
            return
        if self._enviar(texto):
            self._marcar_enviado(chave_dedup)

    def sessao_expirada(self, plataforma: str) -> None:
        # INVARIANTE: reseed é sempre essencial e NUNCA dedupado — só o humano
        # destrava, e engolir este alerta deixaria a conta parada em silêncio.
        self._enviar(f"⚠️ Sessão {plataforma} expirou — refaça o login "
                     f"para a captura voltar a andar.")

    def curso_concluido(self, plataforma: str, curso: str, n: int) -> None:
        # Positivo, raro (1× por curso) => essencial por natureza; dedup por curso
        # protege contra um chamador futuro que repita o evento a cada ciclo.
        texto = (f"✅ {plataforma}: curso '{curso}' concluído — "
                 f"{n} aulas capturadas.")
        chave_dedup = ("curso_concluido", plataforma, curso)
        if self._dedup_repetido(chave_dedup, texto):
            return
        if self._enviar(texto):
            self._marcar_enviado(chave_dedup)


def de_ambiente() -> "Alertas":
    """Constrói do ambiente (.env), no padrão de ponte/config.py. Sem
    TELEGRAM_BOT_TOKEN → modo só-log (fail-safe): a Athena chama alertas mesmo
    sem canal, e o build nunca explode por variável ausente. `ATHENA_ALERTA_NIVEL`
    (essencial|tudo) e `ATHENA_ALERTA_DEDUP_S` calibram o gate/dedup."""
    from dotenv import load_dotenv

    load_dotenv()
    ids = [int(x) for x in os.getenv("TELEGRAM_CHAT_ID", "").replace(" ", "").split(",") if x]
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN ausente — Alertas em modo só-log")
        return Alertas(None, ids)
    return Alertas(TelegramClient(token), ids)
