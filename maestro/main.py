"""Entrypoint (pragma no cover — I/O real). Monta Acesso/Voz/llm e roda o loop
universal sobre os projetos do registro."""
import asyncio
import os
import subprocess

import anthropic
import httpx

from maestro.acesso import Acesso
from maestro.config import carregar
from maestro.registro import carregar as carregar_registro
from maestro.voz import Voz
from maestro import loop
from maestro.athena import Athena, offset_de_arquivo, resolver_offset_path
from maestro.adaptadores import captura
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

    # CAMADA 1 (observabilidade): store durável do estado REAL. Só liga se um DSN
    # próprio da Athena for configurado (OBSERVADOR_DSN) — sem ele, db=None e o
    # loop segue idêntico ao deployado (a observação é opt-in, não quebra nada).
    # Fábrica estilo psycopg3: `with db() as conn`.
    db = None
    dsn = os.getenv("OBSERVADOR_DSN", "")
    if dsn:
        import psycopg
        db = lambda: psycopg.connect(dsn)

    projetos = carregar_registro(cfg.registro_path)

    # CAMADA 3 (interface de comandos no Telegram): listener CONCORRENTE ao loop de
    # saúde. autorizados vem de cfg (fail-closed: vazio => nenhum comando é atendido —
    # NÃO cai pra chat_ids). O offset do Telegram é PERSISTIDO em arquivo (sem isso, um
    # restart reprocessaria comandos antigos). A captura de UM curso pelo /capturar só
    # ENFILEIRA (VPS-safe) — o browser roda no worker residencial. O projeto de captura
    # é o que tem cursos_desejados/adaptador conhecimento; se não houver, o executor
    # fica None e /capturar responde "indisponível" (honesto).
    proj_captura = next((p for p in projetos if getattr(p, "cursos_desejados", ())
                         or p.adaptador == "conhecimento"), None)
    executor = captura.FilaExecutor(acesso, proj_captura) if proj_captura else None

    def captura_fn() -> str:  # pragma: no cover — seção de captura do /status
        return "ver Notion (progresso por curso)"

    athena = None
    if cfg.autorizados:
        tg_athena = TelegramClient(cfg.bot_token)
        athena = Athena(tg_athena, voz, acesso=acesso, executor=executor,
                        captura_fn=captura_fn, autorizados=cfg.autorizados)
    # Offset do Telegram ANCORADO em caminho ABSOLUTO (o dir do registro, estável) —
    # um relativo dependeria do cwd e um restart de outro diretório reprocessaria
    # comandos antigos (a persistência do offset existe justamente p/ evitar isso).
    offset_path = resolver_offset_path(
        os.getenv("ATHENA_OFFSET_PATH"), os.path.dirname(os.path.abspath(cfg.registro_path)))
    offset_seam = offset_de_arquivo(offset_path)

    # Camada 2 (orquestrador) é o DONO PRIMÁRIO do disparo de curso (decisão do dono):
    # ele roda a passada e o `captura.coordenar` é o EXECUTOR (todas as travas — sessão,
    # anti-dup por completude, auto-ingest, Sintetizador, gate I-1), com o gate de
    # plataforma-nova AGORA também no caminho do orquestrador. LIGADO por default;
    # fail-closed: ATHENA_ORQUESTRADOR=0 volta ao caminho coordenar-direto (fallback).
    orquestrar_cursos = os.getenv("ATHENA_ORQUESTRADOR", "1").strip().lower() not in (
        "0", "false", "no", "off", "")

    # CAPACIDADE B (gatilho): plataformas com adaptador. Um curso desejado numa
    # plataforma FORA desta lista é ESCALADO (plataforma nova, sem adaptador) e NÃO
    # capturado — a criação autônoma do adaptador aguarda aprovação humana. Default
    # 'hotmart' (a única que o motor sabe hoje). Vazio => desligado (todos passam).
    plataformas = frozenset(
        p for p in os.getenv("PLATAFORMAS_SUPORTADAS", "hotmart.com").replace(" ", "").split(",") if p)
    plataformas_suportadas = plataformas or None

    asyncio.run(loop.servir(
        acesso, voz, projetos, llm=llm, athena=athena, db=db,
        orquestrar_cursos=orquestrar_cursos, plataformas_suportadas=plataformas_suportadas,
        intervalo_s=cfg.intervalo_s,
        offset_load=offset_seam["offset_load"], offset_save=offset_seam["offset_save"]))


if __name__ == "__main__":  # pragma: no cover
    main()
