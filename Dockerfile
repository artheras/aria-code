# Dockerfile — Aria Code 本地实例
# 用于 docker compose up aria-local

# The Feishu relay and native application runtime are Go binaries; Cloud Run
# runs aria-relay when it sets PORT (scripts/container-entrypoint.sh), while
# local containers use aria-native + aria-runtime.
FROM golang:1.27.3-bookworm AS go-binaries
WORKDIR /src/relay
COPY go/relay/go.mod go/relay/go.sum ./
RUN go mod download
COPY go/relay/*.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /aria-relay .

WORKDIR /src/runtime
COPY go/runtime/go.mod go/runtime/go.sum ./
RUN go mod download
COPY go/runtime/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /aria-runtime .

FROM rust:1.88-bookworm AS native-cli
WORKDIR /src/rust
COPY rust/Cargo.toml rust/Cargo.lock ./
COPY rust/cli ./cli
COPY rust/graph ./graph
COPY rust/physical ./physical
COPY rust/quant ./quant
RUN cargo build --release --locked -p aria-native \
    && cp target/release/aria-native /aria-native

FROM python:3.11-slim

WORKDIR /aria

# System deps for PDF/Excel parsing
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt websockets fastapi "uvicorn[standard]"
COPY --from=go-binaries /aria-relay /usr/local/bin/aria-relay
COPY --from=go-binaries /aria-runtime /usr/local/bin/aria-runtime
COPY --from=native-cli /aria-native /usr/local/bin/aria-native

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
