"""I/O bruto da Bot API (get_updates/send_message). Dublê de http: modela só o
suficiente da resposta da Bot API (result[].message.chat/from/text) pra provar o
parse real, sem abrir rede."""
from maestro.telegram_api import TelegramClient


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeHTTP:
    """Grava os params pedidos (offset) e devolve um payload roteirizado da Bot API."""
    def __init__(self, payload):
        self._payload = payload
        self.gets = []

    def get(self, url, params=None, timeout=None):
        self.gets.append(params)
        return FakeResponse(self._payload)


def _update(update_id, chat_id, chat_type, texto, from_id=None):
    msg = {"chat": {"id": chat_id, "type": chat_type}, "text": texto}
    if from_id is not None:
        msg["from"] = {"id": from_id}
    return {"update_id": update_id, "message": msg}


# --- SEGURANÇA: comando de GRUPO não vira autorização por chat.id compartilhado --
def test_get_updates_descarta_mensagem_de_grupo():
    # DENTE: sem o filtro, uma mensagem de GRUPO vira Update normalmente — e como
    # a autorização da Athena é por chat_id, TODO membro do grupo autorizado
    # comandaria a infra. Rejeitar aqui (na borda de I/O) fecha o buraco: só
    # chat privado (chat.id == user.id) chega à Athena.
    payload = {"result": [
        _update(1, chat_id=-5001, chat_type="group", texto="/restart worker", from_id=777),
    ]}
    http = FakeHTTP(payload)
    tg = TelegramClient("token", http=http)
    out = tg.get_updates(0)
    assert out == []


def test_get_updates_descarta_mensagem_de_supergrupo():
    payload = {"result": [
        _update(1, chat_id=-1001, chat_type="supergroup", texto="/status", from_id=1),
    ]}
    http = FakeHTTP(payload)
    tg = TelegramClient("token", http=http)
    assert tg.get_updates(0) == []


def test_get_updates_aceita_mensagem_privada():
    # Contraprova: chat privado (chat.id == user.id) continua passando normalmente.
    payload = {"result": [
        _update(1, chat_id=100, chat_type="private", texto="/status", from_id=100),
    ]}
    http = FakeHTTP(payload)
    tg = TelegramClient("token", http=http)
    out = tg.get_updates(0)
    assert len(out) == 1
    assert out[0].chat_id == 100 and out[0].texto == "/status"
