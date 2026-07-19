"""Entrypoint (pragma no cover — I/O real). Monta Acesso/Voz/llm e roda o loop
universal sobre os projetos do registro."""
import asyncio
import subprocess

import anthropic
import httpx

from maestro.acesso import Acesso
from maestro.config import carregar
from maestro.registro import carregar as carregar_registro
from maestro.voz import Voz
from maestro import loop
from maestro.telegram_api import TelegramClient


def main():  # pragma: no cover
    cfg = carregar()

    def run_cmd(comando: str, timeout=None) -> str:
        # timeout POR-COMANDO: health checks (docker ps/df/logs/restart) usam o
        # default curto (120s); o reconcile passa um teto generoso (RECONCILE_TIMEOUT_S)
        # porque embeda lote novo no Voyage e enumera ~485 paginas do Notion, o que
        # estoura 120s num curso recem-capturado. None -> 120 (retrocompativel).
        return subprocess.run(["sh", "-c", comando], capture_output=True, text=True,
                              timeout=timeout or 120).stdout

    http = httpx.Client(timeout=60, headers={"Authorization": f"Bearer {cfg.easypanel_token}"})

    def http_post(path: str, body: dict) -> dict:
        r = http.post(cfg.easypanel_url + path, json=body)
        r.raise_for_status()
        return r.json() if r.text else {}

    def probe(url: str) -> bool:
        try:
            return httpx.get(url, timeout=10).is_success
        except Exception:
            return False

    acesso = Acesso(run_cmd=run_cmd, http_post=http_post, probe=probe)
    voz = Voz(TelegramClient(cfg.bot_token), cfg.chat_ids)
    client = anthropic.Anthropic(api_key=cfg.anthropic_key)

    def llm(prompt: str) -> str:
        m = client.messages.create(model=cfg.modelo, max_tokens=1024,
                                   messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in m.content if getattr(b, "type", "") == "text")

    projetos = carregar_registro(cfg.registro_path)
    asyncio.run(loop.run(acesso, voz, projetos, llm=llm, intervalo_s=cfg.intervalo_s))


if __name__ == "__main__":  # pragma: no cover
    main()
