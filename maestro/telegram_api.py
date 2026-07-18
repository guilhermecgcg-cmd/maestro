"""I/O bruto da Bot API do Telegram via httpx (sem lib pesada). Só saída:
get_updates faz long-poll (conexão de saída), send_message envia. Nenhuma porta
de entrada — INVIOLÁVEL 3."""
import time
from dataclasses import dataclass

import httpx

_LIMITE = 4096   # teto de caracteres por mensagem no Telegram


@dataclass
class Update:
    update_id: int
    chat_id: int
    texto: str


class TelegramClient:
    def __init__(self, bot_token: str, http=None, sleep=time.sleep):
        self._base = f"https://api.telegram.org/bot{bot_token}"
        self._http = http or httpx.Client(timeout=60)
        self._sleep = sleep   # injetável p/ teste (retry sem gastar tempo real)

    def get_updates(self, offset: int, timeout: int = 25) -> list[Update]:
        r = self._http.get(f"{self._base}/getUpdates",
                           params={"offset": offset, "timeout": timeout},
                           timeout=timeout + 10)
        r.raise_for_status()
        out: list[Update] = []
        for u in r.json().get("result", []):
            msg = u.get("message")            # só mensagens novas (não edições)
            if not msg or "text" not in msg:
                continue
            out.append(Update(update_id=u["update_id"],
                             chat_id=msg["chat"]["id"], texto=msg["text"]))
        return out

    def send_message(self, chat_id: int, texto: str, *, max_retries: int = 3) -> None:
        """Envia (em pedaços de <=4096). Retry com backoff em 429 (respeita
        retry_after) e 5xx — o canal de ESCALAÇÃO não pode falhar num hiccup."""
        for i in range(0, len(texto), _LIMITE):
            pedaco = texto[i:i + _LIMITE]
            for tent in range(max_retries + 1):
                r = self._http.post(f"{self._base}/sendMessage",
                                   json={"chat_id": chat_id, "text": pedaco}, timeout=30)
                if r.status_code == 429 and tent < max_retries:
                    espera = 1
                    try:
                        espera = min(int(r.json().get("parameters", {}).get("retry_after", 1)), 60)
                    except Exception:
                        pass
                    self._sleep(espera)
                    continue
                if r.status_code >= 500 and tent < max_retries:
                    self._sleep(2 ** tent)
                    continue
                r.raise_for_status()
                break
