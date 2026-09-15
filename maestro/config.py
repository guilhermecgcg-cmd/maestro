"""Config do Maestro (padrão de dataclass frozen). Sem DATABASE_URL próprio — o
banco de cada projeto vem do REGISTRO."""
import logging
import math
import os
from dataclasses import dataclass

from dotenv import load_dotenv

log = logging.getLogger("maestro.config")


def _env_float_tolerante(nome, padrao, *, minimo):
    """Leitura TOLERANTE de uma env numérica (D3, item 6 — achado 5 da revisão independente):
    ausente/vazia => `padrao`; ilegível, nan ou inf => `padrao` com WARNING; abaixo de `minimo`
    => `minimo` com WARNING. O `main()` do daemon chama `carregar()` na partida: um typo no
    launch.sh (MAESTRO_INTERVALO_S="dois minutos") derrubava o daemon antes do 1º ciclo."""
    bruto = os.getenv(nome)
    if bruto is None or not str(bruto).strip():
        return float(padrao)
    try:
        valor = float(str(bruto).strip())
    except ValueError:
        valor = None
    if valor is None or not math.isfinite(valor):
        log.warning("%s=%r não é um número finito — usando o default %s", nome, bruto, padrao)
        return float(padrao)
    if valor < minimo:
        log.warning("%s=%s abaixo do mínimo %s — usando %s", nome, valor, minimo, minimo)
        return float(minimo)
    return valor


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
    autorizados: frozenset  # SEGURANÇA: chats que podem comandar a Athena via
    # Telegram (Athena(autorizados=...)). Fail-closed: vazio = nenhum comando é
    # atendido — não existe "sem restrição". Vem de TELEGRAM_AUTORIZADOS; sem
    # essa env, NÃO cai pra chat_ids automaticamente (broadcast != autorização
    # de comando são papéis distintos) — fica vazio de propósito.


def carregar() -> MaestroConfig:
    load_dotenv()
    ids = frozenset(int(x) for x in os.getenv("TELEGRAM_CHAT_ID", "").replace(" ", "").split(",") if x)
    autorizados = frozenset(int(x) for x in os.getenv("TELEGRAM_AUTORIZADOS", "").replace(" ", "").split(",") if x)
    return MaestroConfig(
        bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        chat_ids=ids,
        anthropic_key=os.environ["ANTHROPIC_API_KEY"],
        easypanel_url=os.getenv("EASYPANEL_URL", "http://127.0.0.1:3000"),
        easypanel_token=os.getenv("EASYPANEL_TOKEN", ""),
        registro_path=os.getenv("REGISTRO_PATH", "projetos.yaml"),
        intervalo_s=_env_float_tolerante("MAESTRO_INTERVALO_S", 120.0, minimo=1.0),
        modelo=os.getenv("MAESTRO_MODELO", "claude-opus-4-8"),
        autorizados=autorizados,
    )
