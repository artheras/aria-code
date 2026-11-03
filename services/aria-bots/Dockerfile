# services/aria-bots — the chat bots as their own image.
#
#   docker build -t aria-bots services/aria-bots
#   docker run --env-file ~/.aria/.env aria-bots            # relay client
#
# The relay client connects out to the Go relay (Dockerfile.relay) and answers
# the Feishu events it forwards; it needs no inbound port.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[files]" \
 && useradd --create-home --uid 10001 aria
USER aria
ENTRYPOINT ["aria-relay-client"]
