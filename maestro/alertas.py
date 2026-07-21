"""Alertas de supervisão da Athena via Telegram. A Athena chama estas funções em
eventos de supervisão — captura morreu, sessão expirou, curso concluído — e o dono
recebe no Telegram. Reusa o TelegramClient da ponte (retry/429 já embutido). Só
SAÍDA: nenhuma porta de entrada nova (INVIOLÁVEL 3).

Fail-safe é a regra central deste módulo: sem token/cliente, o alerta vira log e
NUNCA levanta. O canal que existe para avisar que o supervisor caiu jamais pode ser
ele próprio quem derruba o supervisor — por isso todo envio é best-effort e toda
exceção do Telegram é engolida (a ponte já tem retry para o hiccup transitório)."""
import logging
import os

from maestro.telegram_api import TelegramClient

log = logging.getLogger("athena.alertas")


class Alertas:
    """Emite alertas de supervisão. `tg=None` => modo só-log (fail-safe), para a
    Athena poder chamar alertas mesmo sem canal Telegram configurado."""

    def __init__(self, tg, chat_ids):
        self._tg = tg
        self._chats = list(chat_ids)

    def _enviar(self, texto: str) -> None:
        if self._tg is None or not self._chats:
            log.warning("alerta sem canal Telegram (só-log): %s", texto)
            return
        for chat in self._chats:
            try:
                self._tg.send_message(chat, texto)
            except Exception:
                # best-effort: o alerta nunca pode derrubar quem o chamou.
                log.exception("falha ao enviar alerta para chat %s", chat)

    def captura_morreu(self, plataforma: str, motivo: str) -> None:
        self._enviar(f"🔴 Captura {plataforma} MORREU — {motivo}. "
                     f"Fila sem supervisão; retomar exige olhar humano.")

    def sessao_expirada(self, plataforma: str) -> None:
        self._enviar(f"⚠️ Sessão {plataforma} expirou — refaça o login "
                     f"para a captura voltar a andar.")

    def curso_concluido(self, plataforma: str, curso: str, n: int) -> None:
        self._enviar(f"✅ {plataforma}: curso '{curso}' concluído — "
                     f"{n} aulas capturadas.")


def de_ambiente() -> "Alertas":
    """Constrói do ambiente (.env), no padrão de ponte/config.py. Sem
    TELEGRAM_BOT_TOKEN → modo só-log (fail-safe): a Athena chama alertas mesmo
    sem canal, e o build nunca explode por variável ausente."""
    from dotenv import load_dotenv

    load_dotenv()
    ids = [int(x) for x in os.getenv("TELEGRAM_CHAT_ID", "").replace(" ", "").split(",") if x]
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN ausente — Alertas em modo só-log")
        return Alertas(None, ids)
    return Alertas(TelegramClient(token), ids)
