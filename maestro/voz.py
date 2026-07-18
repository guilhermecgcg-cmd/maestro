"""Voz do Maestro no Telegram: reporta cada ação (avisa-e-age), escala com pedido
claro, e interpreta comandos em português. Reusa o TelegramClient da ponte (retry/429
já embutido). Só SAÍDA + comandos — nenhuma porta de entrada nova (INVIOLÁVEL 3)."""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Comando:
    tipo: str
    arg: str


class Voz:
    def __init__(self, tg, chat_ids):
        self._tg = tg
        self._chats = list(chat_ids)

    def _enviar(self, texto: str) -> None:
        for chat in self._chats:
            try:
                self._tg.send_message(chat, texto)
            except Exception:
                pass  # best-effort; a ponte já tem retry

    def avisar_acao(self, acao) -> None:
        if acao.executada:
            self._enviar(f"🔧 {acao.descricao}")

    def escalar(self, problema, pedido: str) -> None:
        self._enviar(f"⚠️ {pedido}\n(origem: {problema.tipo} em {problema.alvo})")

    def interpretar(self, texto: str) -> Comando:
        t = texto.strip().lower()
        if t == "status":
            return Comando("status", "")
        m = re.match(r"captura\s+(\S+)", texto.strip(), re.I)
        if m:
            return Comando("captura", m.group(1))
        m = re.match(r"por\s*qu[eê].*?(\S+)\??$", texto.strip(), re.I)
        if m:
            return Comando("porque", m.group(1))
        return Comando("desconhecido", texto.strip())
