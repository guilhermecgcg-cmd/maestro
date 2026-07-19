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
    def __init__(self, *, run_cmd=None, http_get=None, http_post=None, probe=None):
        self._run_cmd = run_cmd
        self._http_get = http_get
        self._http_post = http_post
        self._probe = probe

    def descobrir_projetos(self) -> dict:
        """Auto-descoberta: TODOS os projetos+serviços do Easypanel. Deploy novo
        aparece aqui sozinho — o Maestro não depende de registro manual pra SABER
        que um sistema existe. Retorna {projeto_easypanel: [serviços]}."""
        data = self._http_get("/api/trpc/projects.listProjectsAndServices")
        out = {}
        for svc in (data.get("json", {}) or {}).get("services", []):
            out.setdefault(svc["projectName"], []).append(svc["name"])
        return out

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

    def exec_sql(self, container_match: str, sql: str, *, db: str,
                 user: str = "postgres", rows: bool = True) -> list:
        """Roda SQL DENTRO de um container Postgres via socket (`docker exec ... psql`).
        Vence o isolamento de rede entre projetos do Easypanel: o Maestro, externo ao
        projeto, alcança o banco de DENTRO do container do próprio projeto. Sem senha
        (trust local no socket unix do postgres). Linhas -tA: campos separados por '|'.

        LEVANTA em qualquer falha (container inexistente, auth, erro de psql). Sem
        isso, falha viraria stdout vazio == 'fila limpa' — ponto cego que deixaria
        job travado sem vigilância (risco de queimar conta paga). Usa uma sentinela
        __EXEC_OK__ como última linha só quando o psql retorna 0."""
        q = shlex.quote
        inner = (f'docker exec -i "$CID" psql -U {q(user)} -d {q(db)} -tAqc {q(sql)}')
        cmd = (f'CID=$(docker ps -qf name={q(container_match)} | head -1); '
               f'if [ -z "$CID" ]; then echo __EXEC_FAIL__no_container; '
               f'else {inner} && echo __EXEC_OK__ || echo __EXEC_FAIL__psql; fi')
        saida = self._run_cmd(cmd) or ""
        linhas = saida.splitlines()
        if not linhas or linhas[-1].strip() != "__EXEC_OK__":
            raise RuntimeError(
                f"exec_sql falhou em {container_match}/{db}: {saida[-200:]!r}")
        if not rows:
            return []
        return [ln for ln in linhas[:-1] if ln.strip()]

    def exec_app(self, container_match: str, comando: str) -> str:
        """Roda um comando ARBITRARIO dentro de um container de APP (nao-psql) via
        `docker exec`, descobrindo o container pelo socket (`docker ps`). Captura
        stdout+stderr (2>&1) para o chamador confirmar sucesso/erro pela saida.
        LEVANTA se o container nao existe -- silencio nao pode virar falso 'ok'
        (mesma disciplina do exec_sql). `comando` e CODIGO-controlado (literal fixo
        no adaptador), nunca entrada de usuario -- por isso vai cru (com args)."""
        q = shlex.quote
        cmd = (f'CID=$(docker ps -qf name={q(container_match)} | head -1); '
               f'if [ -z "$CID" ]; then echo __EXEC_FAIL__no_container; '
               f'else docker exec -i "$CID" {comando} 2>&1; fi')
        saida = self._run_cmd(cmd) or ""
        if "__EXEC_FAIL__no_container" in saida:
            raise RuntimeError(f"exec_app: container {container_match!r} nao encontrado")
        return saida

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
