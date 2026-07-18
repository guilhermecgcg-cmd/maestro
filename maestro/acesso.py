"""Olhos e mãos GENÉRICOS do Maestro no runtime — sem acoplar a projeto nenhum.
Seams injetáveis (run_cmd/http_post/probe). docker ps enxerga TODOS os containers
do host; redeploy recebe o projeto Easypanel (não é chumbado). Dados específicos de
projeto (ex.: fila de captura do conhecimento) ficam em ADAPTADORES, fora daqui."""
import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class Servico:
    nome: str
    up: bool
    restarting: bool


def _nome_curto(container: str) -> str:
    base = container.split(".")[0]
    return base.split("_", 1)[1] if "_" in base else base


class Acesso:
    def __init__(self, *, run_cmd=None, http_post=None, probe=None):
        self._run_cmd = run_cmd
        self._http_post = http_post
        self._probe = probe

    def servicos(self) -> dict:
        saida = self._run_cmd('docker ps -a --format "{{.Status}}  {{.Names}}"')
        out = {}
        for linha in saida.splitlines():
            if not linha.strip():
                continue
            status, _, container = linha.partition("  ")
            nome = _nome_curto(container.strip())
            out[nome] = Servico(nome=nome, up=status.startswith("Up"),
                                restarting=status.startswith("Restarting"))
        return out

    def logs(self, nome: str, n: int = 50) -> str:
        return self._run_cmd(
            f"docker logs --tail {int(n)} $(docker ps -aqf name={shlex.quote(nome)} | head -1) 2>&1")

    def restart(self, nome: str) -> None:
        self._run_cmd(f"docker restart $(docker ps -aqf name={shlex.quote(nome)} | head -1)")

    def redeploy(self, nome: str, projeto_easypanel: str) -> None:
        self._http_post("/api/trpc/services.app.deployService",
                        {"json": {"projectName": projeto_easypanel, "serviceName": nome}})

    def saude_http(self, alvos: dict) -> dict:
        out = {}
        for nome, url in alvos.items():
            try:
                out[nome] = bool(self._probe(url))
            except Exception:
                out[nome] = False
        return out

    def recursos(self) -> dict:
        saida = self._run_cmd("df -P / | tail -1; free | awk '/Mem:/{print $3/$2*100}'")
        linhas = saida.split("\n")
        campos = linhas[0].split() if linhas else []
        disco = float(campos[4].rstrip("%")) if len(campos) >= 5 else 0.0
        ram = float(linhas[1]) if len(linhas) > 1 and linhas[1].strip() else 0.0
        return {"disco_pct": disco, "ram_pct": ram}
