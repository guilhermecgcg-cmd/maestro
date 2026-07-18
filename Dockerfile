# Maestro — coordenador universal. Usa /var/run/docker.sock (montado) pra ops.
FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends docker.io \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen
COPY . .
CMD ["uv", "run", "python", "-m", "maestro.main"]
