# Maestro — coordenador universal. Usa /var/run/docker.sock (montado) pra ops.
FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
# CLI do docker para operar o socket montado. `docker --version` é um GUARD de
# build: se o docker NÃO instalar, o build FALHA ALTO (não sobe imagem sem docker,
# que foi o bug — a imagem subia sem o CLI e todo `docker exec` do acesso quebrava).
RUN apt-get update && apt-get install -y --no-install-recommends docker.io \
    && rm -rf /var/lib/apt/lists/* \
    && docker --version
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen
COPY . .
CMD ["uv", "run", "python", "-m", "maestro.main"]
