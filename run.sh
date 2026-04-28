#!/usr/bin/env bash
# sysdash launcher: sets up venv with python3.12, installs deps, runs server.
# Server picks a free port and writes it to ~/.sysdash-port, then opens browser.

set -euo pipefail
cd "$(dirname "$0")"

# ---- Pick Python 3.12 explicitly ----
PYTHON=""
for candidate in /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12 python3.12; do
  if command -v "$candidate" &>/dev/null; then
    PYTHON="$candidate"
    break
  fi
done
if [ -z "$PYTHON" ]; then
  echo "[sysdash] ERROR: python3.12 not found. Install with: brew install python@3.12" >&2
  exit 1
fi
echo "[sysdash] using $PYTHON ($($PYTHON --version 2>&1))"

# ---- Fix Homebrew expat linkage (pyexpat crash on macOS) ----
EXPAT_LIB="/opt/homebrew/opt/expat/lib"
if [ -d "$EXPAT_LIB" ]; then
  export DYLD_LIBRARY_PATH="${EXPAT_LIB}${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
fi

# ---- Create or rebuild venv ----
VENV=".venv"
_needs_rebuild=0

if [ ! -d "$VENV" ]; then
  _needs_rebuild=1
elif [ ! -x "$VENV/bin/python" ]; then
  _needs_rebuild=1
elif ! "$VENV/bin/python" -c "import sys; assert sys.version_info[:2] == (3,12)" 2>/dev/null; then
  echo "[sysdash] venv is not python3.12 — rebuilding..."
  _needs_rebuild=1
fi

if [ "$_needs_rebuild" -eq 1 ]; then
  rm -rf "$VENV"
  echo "[sysdash] creating venv..."
  if ! "$PYTHON" -m venv "$VENV" 2>/dev/null; then
    echo "[sysdash] ensurepip failed — retrying with --without-pip + get-pip.py..."
    "$PYTHON" -m venv --without-pip "$VENV"
    curl -sS https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
    "$VENV/bin/python" /tmp/get-pip.py
    rm -f /tmp/get-pip.py
  fi
  # Force deps reinstall after rebuild
  rm -f "$VENV/.deps_installed"
fi

# ---- Activate ----
# shellcheck disable=SC1091
source "$VENV/bin/activate"

# ---- Install deps if needed ----
if [ ! -f "$VENV/.deps_installed" ] || [ requirements.txt -nt "$VENV/.deps_installed" ]; then
  echo "[sysdash] installing dependencies..."
  pip install --quiet --upgrade pip
  pip install --quiet -r requirements.txt
  touch "$VENV/.deps_installed"
fi

# ---- Sanity check ----
if ! python -c "import psutil, fastapi, uvicorn" 2>/dev/null; then
  echo "[sysdash] ERROR: imports failed after install. Check requirements.txt" >&2
  exit 1
fi

# ---- Launch ----
exec python server.py
