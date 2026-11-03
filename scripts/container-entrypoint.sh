#!/bin/sh
# Cloud Run supplies PORT. Local CLI containers do not.
set -eu

if [ -n "${PORT:-}" ]; then
  exec aria-relay
fi

exec aria-code "$@"
