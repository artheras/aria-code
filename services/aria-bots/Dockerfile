# services/aria-bots — the chat bots as their own image, with the Go engine.
#
#   docker build -f services/aria-bots/Dockerfile -t aria-bots .
#   docker run --env-file ~/.aria/.env -v aria-bots:/home/aria/.aria aria-bots
#
# Built from the repository root: the image carries aria-runtime (go/runtime),
# which answers chat turns, beside the bots. The relay client connects out to
# the Go relay (Dockerfile.relay) and needs no inbound port.
FROM golang:1.27.3-bookworm AS engine
WORKDIR /src
COPY go/runtime/go.mod go/runtime/go.sum ./
RUN go mod download
COPY go/runtime/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /aria-runtime .

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY services/aria-bots/pyproject.toml services/aria-bots/README.md ./
COPY services/aria-bots/src ./src
RUN pip install --no-cache-dir ".[files]" \
 && useradd --create-home --uid 10001 aria
COPY --from=engine /aria-runtime /usr/local/bin/aria-runtime
USER aria
ENTRYPOINT ["aria-relay-client"]
