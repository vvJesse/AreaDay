#!/bin/sh
set -eu

UV_VERSION="0.12.6"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SKILL_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
RUNTIME_DIR=${RESEARCHRAMP_RUNTIME_DIR:-"$SKILL_DIR/.runtime"}
VENV_DIR=${RESEARCHRAMP_VENV_DIR:-"$SKILL_DIR/.venv"}
MODEL_DIR=${RESEARCHRAMP_MODEL_DIR:-"$HOME/.researchramp/models/sentence-transformers"}
SETUP_SCRIPT="$SCRIPT_DIR/setup_dependencies.py"
OPENALEX_SETUP_SCRIPT="$SCRIPT_DIR/configure_openalex.sh"
OPENALEX_CONFIG="$HOME/.researchramp/credentials.ini"
MODE=${1:---install}
OPENALEX_SETUP_PID=""

stop_openalex_setup() {
  if [ -n "$OPENALEX_SETUP_PID" ]; then
    kill "$OPENALEX_SETUP_PID" >/dev/null 2>&1 || true
    wait "$OPENALEX_SETUP_PID" >/dev/null 2>&1 || true
    OPENALEX_SETUP_PID=""
  fi
}

wait_for_openalex_setup() {
  if [ -n "$OPENALEX_SETUP_PID" ]; then
    if ! wait "$OPENALEX_SETUP_PID"; then
      OPENALEX_SETUP_PID=""
      echo "OpenAlex setup did not complete." >&2
      return 1
    fi
    OPENALEX_SETUP_PID=""
  fi
  if [ ! -f "$OPENALEX_CONFIG" ]; then
    echo "OpenAlex setup did not create $OPENALEX_CONFIG" >&2
    return 1
  fi
}

trap stop_openalex_setup EXIT
trap 'stop_openalex_setup; exit 130' HUP INT TERM

case "$MODE" in
  --check|--install|--bootstrap-only) ;;
  *)
    echo "Usage: sh scripts/install.sh [--check|--install|--bootstrap-only]" >&2
    exit 2
    ;;
esac

if [ "$MODE" = "--check" ]; then
  VENV_PYTHON="$VENV_DIR/bin/python"
  if [ ! -x "$VENV_PYTHON" ]; then
    echo "ResearchRamp runtime is not installed at $VENV_DIR" >&2
    exit 1
  fi
  exec "$VENV_PYTHON" "$SETUP_SCRIPT" \
    --venv-dir "$VENV_DIR" \
    --model-dir "$MODEL_DIR"
fi

if [ "$MODE" = "--install" ]; then
  sh "$OPENALEX_SETUP_SCRIPT" &
  OPENALEX_SETUP_PID=$!
fi

mkdir -p "$RUNTIME_DIR"
LOCAL_UV_DIR="$RUNTIME_DIR/uv"
LOCAL_UV="$LOCAL_UV_DIR/uv"

if [ -x "$LOCAL_UV" ]; then
  UV_BIN="$LOCAL_UV"
else
  INSTALLER_URL=${RESEARCHRAMP_UV_INSTALLER_URL:-"https://astral.sh/uv/$UV_VERSION/install.sh"}
  UV_ARTIFACT_BASES=${RESEARCHRAMP_UV_DOWNLOAD_URL:-"https://github.com/astral-sh/uv/releases/download/$UV_VERSION https://releases.astral.sh/github/uv/releases/download/$UV_VERSION"}
  INSTALLER_PATH="$RUNTIME_DIR/uv-installer-$UV_VERSION.sh"
  echo "Downloading the pinned uv $UV_VERSION installer..."
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf --connect-timeout 15 --max-time 120 "$INSTALLER_URL" -o "$INSTALLER_PATH"
  elif command -v wget >/dev/null 2>&1; then
    wget --timeout=120 -qO "$INSTALLER_PATH" "$INSTALLER_URL"
  else
    echo "Cannot bootstrap uv: neither curl nor wget is available." >&2
    exit 1
  fi
  UV_UNMANAGED_INSTALL="$LOCAL_UV_DIR" \
    UV_NO_MODIFY_PATH=1 \
    UV_DISABLE_UPDATE=1 \
    UV_DOWNLOAD_URL="$UV_ARTIFACT_BASES" \
    sh "$INSTALLER_PATH"
  if [ ! -x "$LOCAL_UV" ]; then
    echo "The uv installer completed without creating $LOCAL_UV" >&2
    exit 1
  fi
  UV_BIN="$LOCAL_UV"
fi

"$UV_BIN" --version
if [ "$MODE" = "--bootstrap-only" ]; then
  exit 0
fi

UV_BIN_DIR=$(dirname -- "$UV_BIN")
PATH="$UV_BIN_DIR:$PATH"
UV_PYTHON_INSTALL_DIR="$RUNTIME_DIR/python"
UV_CACHE_DIR="$RUNTIME_DIR/cache"
export PATH UV_PYTHON_INSTALL_DIR UV_CACHE_DIR

if "$UV_BIN" run --isolated --no-project --no-config --managed-python --python 3.12 "$SETUP_SCRIPT" \
  --install \
  --venv-dir "$VENV_DIR" \
  --model-dir "$MODEL_DIR"; then
  wait_for_openalex_setup
  echo "Installation verified; removing the disposable package-download cache..."
  if ! "$UV_BIN" cache clean --no-config; then
    echo "Warning: installation succeeded, but the disposable uv cache could not be cleaned." >&2
  fi
  exit 0
else
  exit $?
fi
