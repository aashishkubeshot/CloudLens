#!/usr/bin/env bash
# Set up cloudlens and launch the TUI in this terminal.
# Usage: ./start.sh [-s svc1,svc2] [--hours N]
set -euo pipefail

cd "$(dirname "$0")"

# --- project from .env ------------------------------------------------------
if [[ ! -f .env ]]; then
  echo "No .env found. Copy .env.example to .env and set GOOGLE_CLOUD_PROJECT:"
  echo "  cp .env.example .env && \$EDITOR .env"
  exit 1
fi
set -a
# shellcheck disable=SC1091
source .env
set +a
: "${GOOGLE_CLOUD_PROJECT:?GOOGLE_CLOUD_PROJECT must be set in .env}"

# --- prereqs ----------------------------------------------------------------
command -v python3 >/dev/null || { echo "python3 not found"; exit 1; }
command -v gcloud  >/dev/null || { echo "gcloud not found — install the Google Cloud SDK"; exit 1; }

ADC="$HOME/.config/gcloud/application_default_credentials.json"
if [[ ! -f "$ADC" ]]; then
  echo "Application Default Credentials not found."
  echo "Run:  gcloud auth application-default login"
  exit 1
fi

# --- venv + install (skip reinstall if pyproject unchanged) -----------------
if [[ ! -d .venv ]]; then
  echo "Creating virtualenv .venv …"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

STAMP=.venv/.installed-hash
CURRENT_HASH=$(shasum pyproject.toml | awk '{print $1}')
if ! command -v cloudlens-watch >/dev/null \
   || [[ ! -f $STAMP ]] \
   || [[ "$(cat "$STAMP")" != "$CURRENT_HASH" ]]; then
  echo "Installing cloudlens …"
  pip install --quiet --upgrade pip
  pip install --quiet -e .
  echo "$CURRENT_HASH" > "$STAMP"
fi

# --- launch -----------------------------------------------------------------
echo "Starting cloudlens-watch on project: $GOOGLE_CLOUD_PROJECT"
exec cloudlens-watch "$@"
