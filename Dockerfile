# Maestro — coordenador universal. Usa /var/run/docker.sock (montado) pra ops.
FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
# CLI do docker (BINÁRIO ESTÁTICO) p/ operar o socket montado. O pacote docker.io
# do apt (Debian 13) instala o DAEMON mas NÃO o binário `docker` no PATH (com
# --no-install-recommends) — era o bug: imagem subia sem CLI e todo docker exec do
# acesso quebrava com no_container. O estático é determinístico (detecta a arch).
# `docker --version` = GUARD: o build FALHA ALTO se o docker não ficar utilizável.
RUN ARCH=$(uname -m) \
    && python3 -c "import urllib.request,sys; urllib.request.urlretrieve('https://download.docker.com/linux/static/stable/'+sys.argv[1]+'/docker-27.3.1.tgz','/tmp/d.tgz')" "$ARCH" \
    && tar -xzf /tmp/d.tgz -C /usr/local/bin --strip-components=1 docker/docker \
    && rm /tmp/d.tgz \
    && docker --version
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen
COPY . .
CMD ["uv", "run", "python", "-m", "maestro.main"]
