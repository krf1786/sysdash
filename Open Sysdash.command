#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.sysdash.agent"
DOMAIN="gui/$(id -u)"
PORT_FILE="$HOME/.sysdash-port"

cd "$DIR"

if ! launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  ./install-autostart.sh
else
  launchctl kickstart -k "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
fi

for _ in {1..50}; do
  PORT="$(cat "$PORT_FILE" 2>/dev/null || true)"
  if [ -n "$PORT" ] && nc -z 127.0.0.1 "$PORT" >/dev/null 2>&1; then
    open "http://localhost:$PORT/"
    exit 0
  fi
  sleep 0.2
done

PORT="$(cat "$PORT_FILE" 2>/dev/null || true)"
if [ -n "$PORT" ]; then
  open "http://localhost:$PORT/"
else
  open "http://localhost:55067/"
fi
