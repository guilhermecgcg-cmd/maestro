"""Config do Maestro (padrão de dataclass frozen). Sem DATABASE_URL próprio — o
banco de cada projeto vem do REGISTRO."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class MaestroConfig:
    bot_token: str
    chat_ids: frozenset
    anthropic_key: str
    easypanel_url: str
    easypanel_token: str
    registro_path: str
    intervalo_s: float
    modelo: str


def carregar() -> MaestroConfig:
    load_dotenv()
    ids = frozenset(int(x) for x in os.getenv("TELEGRAM_CHAT_ID", "").replace(" ", "").split(",") if x)
    return MaestroConfig(
        bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        chat_ids=ids,
        anthropic_key=os.environ["ANTHROPIC_API_KEY"],
        easypanel_url=os.getenv("EASYPANEL_URL", "http://127.0.0.1:3000"),
        easypanel_token=os.getenv("EASYPANEL_TOKEN", ""),
        registro_path=os.getenv("REGISTRO_PATH", "projetos.yaml"),
        intervalo_s=float(os.getenv("MAESTRO_INTERVALO_S", "120")),
        modelo=os.getenv("MAESTRO_MODELO", "claude-opus-4-8"),
    )
