# Dockerfile — Aria Code 本地实例
# 用于 docker compose up aria-local

# The Feishu relay is a Go binary; Cloud Run runs it from this image when it
# sets PORT (scripts/container-entrypoint.sh).
FROM golang:1.27.3-bookworm AS relay
WORKDIR /src
COPY go/relay/go.mod go/relay/go.sum ./
RUN go mod download
COPY go/relay/*.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /aria-relay .

FROM python:3.11-slim

WORKDIR /aria

# System deps for PDF/Excel parsing
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt websockets fastapi "uvicorn[standard]"
COPY --from=relay /aria-relay /usr/local/bin/aria-relay

COPY . .
# Installs the console scripts (aria-code, aria-code-mcp) and puts aria_code on
# the path, which every service module now imports from.
RUN pip install --no-cache-dir --no-deps -e .

# Config volume mount point
RUN mkdir -p /root/.aria

ENV PYTHONUNBUFFERED=1

EXPOSE 8080

# The managed Cloud Run trigger builds this root Dockerfile. Cloud Run sets
# PORT; local CLI containers keep the original aria-code command.
CMD ["sh", "scripts/container-entrypoint.sh"]
