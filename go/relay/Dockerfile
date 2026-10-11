# Go relay image: a static binary on distroless, for Cloud Run like Dockerfile.relay.
#   docker build -t aria-relay-go go/relay
# Same environment as the Python relay (PORT, DB_PATH, RELAY_STORE, FEISHU_*, ...).
# Production still deploys Dockerfile.relay; switching is a separate step
# (docs/rust-cli-migration.md).

FROM golang:1.26-bookworm AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY *.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /aria-relay .
RUN mkdir /data && chown 65532:65532 /data

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /aria-relay /aria-relay
COPY --from=build --chown=65532:65532 /data/ /data/
ENV PORT=8765 DB_PATH=/tmp/relay.db
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD ["/aria-relay", "--healthcheck"]
ENTRYPOINT ["/aria-relay"]
