#!/bin/sh
set -eu

UV_VERSION="0.12.6"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SKILL_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
RUNTIME_DIR=${RESEARCHRAMP_RUNTIME_DIR:-"$SKILL_DIR/.runtime"}
VENV_DIR=${RESEARCHRAMP_VENV_DIR:-"$SKILL_DIR/.venv"}
MODEL_DIR=${RESEARCHRAMP_MODEL_DIR:-"$HOME/.researchramp/models/sentence-transformers"}
SETUP_SCRIPT="$SCRIPT_DIR/setup_dependencies.py"
MIGRATION_SCRIPT="$SCRIPT_DIR/migrate_areaday_data.py"
OPENALEX_SETUP_SCRIPT="$SCRIPT_DIR/configure_openalex.sh"
OPENALEX_CONFIG="$HOME/.researchramp/credentials.ini"
MODE=${1:---install}
OPENALEX_SETUP_PID=""
RUNTIME_STAGE=""

stop_openalex_setup() {
  if [ -n "$OPENALEX_SETUP_PID" ]; then
    kill "$OPENALEX_SETUP_PID" >/dev/null 2>&1 || true
    wait "$OPENALEX_SETUP_PID" >/dev/null 2>&1 || true
    OPENALEX_SETUP_PID=""
  fi
}

cleanup_runtime_stage() {
  if [ -n "$RUNTIME_STAGE" ] && [ -d "$RUNTIME_STAGE" ]; then
    rm -rf -- "$RUNTIME_STAGE"
    RUNTIME_STAGE=""
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

trap 'cleanup_runtime_stage; stop_openalex_setup' EXIT
trap 'cleanup_runtime_stage; stop_openalex_setup; exit 130' HUP INT TERM

case "$MODE" in
  --check|--install|--bootstrap-only|--runtime-only) ;;
  *)
    echo "Usage: sh scripts/install.sh [--check|--install|--bootstrap-only|--runtime-only]" >&2
    exit 2
    ;;
esac

if [ "$MODE" = "--check" ]; then
  VENV_PYTHON="$VENV_DIR/bin/python"
  if [ ! -x "$VENV_PYTHON" ]; then
    echo "AreaDay runtime is not installed at $VENV_DIR" >&2
    exit 1
  fi
  exec "$VENV_PYTHON" "$SETUP_SCRIPT" \
    --venv-dir "$VENV_DIR" \
    --model-dir "$MODEL_DIR"
fi

case "$(uname -s)-$(uname -m)" in
  Darwin-arm64|Darwin-aarch64) PLATFORM_ID="macos-arm64" ;;
  *)
    echo "This AreaDay installer supports Apple silicon Macs only." >&2
    exit 1
    ;;
esac

find_bundled_runtime() {
  RUNTIME_PACK_DIR="$SKILL_DIR/runtime-packs"
  [ -d "$RUNTIME_PACK_DIR" ] || return 1
  RUNTIME_MATCHES=$(find "$RUNTIME_PACK_DIR" -maxdepth 1 -type f \
    -name "AreaDay-runtime-$PLATFORM_ID-*.zip" -print)
  RUNTIME_COUNT=$(printf '%s\n' "$RUNTIME_MATCHES" | sed '/^$/d' | wc -l | tr -d ' ')
  if [ "$RUNTIME_COUNT" -eq 0 ]; then
    return 1
  fi
  if [ "$RUNTIME_COUNT" -ne 1 ]; then
    echo "Expected one bundled runtime for $PLATFORM_ID, found $RUNTIME_COUNT." >&2
    return 2
  fi
  BUNDLED_RUNTIME=$RUNTIME_MATCHES
}

install_bundled_runtime() {
  runtime_archive=$1
  mkdir -p "$RUNTIME_DIR"
  RUNTIME_STAGE=$(mktemp -d "$RUNTIME_DIR/areaday-runtime-stage.XXXXXX")
  echo "Installing the bundled AreaDay runtime for $PLATFORM_ID..."
  ditto -x -k "$runtime_archive" "$RUNTIME_STAGE"
  staged_root="$RUNTIME_STAGE/runtime"
  staged_venv="$staged_root/venv"
  staged_model="$staged_root/models/sentence-transformers"
  staged_python="$staged_venv/bin/python"
  staged_manifest="$staged_root/runtime.json"
  if [ ! -x "$staged_python" ] || [ ! -f "$staged_manifest" ] || [ ! -d "$staged_model" ]; then
    echo "The bundled runtime is incomplete." >&2
    return 1
  fi
  "$staged_python" -c 'import json,sys; p=json.load(open(sys.argv[1], encoding="utf-8")); assert p.get("schema_version")==1 and p.get("product")=="areaday" and p.get("platform")==sys.argv[2]' "$staged_manifest" "$PLATFORM_ID"
  "$staged_python" "$SETUP_SCRIPT" --venv-dir "$staged_venv" --model-dir "$staged_model"

  mkdir -p "$(dirname -- "$MODEL_DIR")" "$(dirname -- "$VENV_DIR")"
  ditto "$staged_model" "$MODEL_DIR"
  backup_venv="$VENV_DIR.areaday-backup-$$"
  if [ -e "$backup_venv" ]; then
    echo "Cannot create the temporary runtime backup: $backup_venv already exists." >&2
    return 1
  fi
  had_previous_venv=0
  if [ -e "$VENV_DIR" ]; then
    mv "$VENV_DIR" "$backup_venv"
    had_previous_venv=1
  fi
  if mv "$staged_venv" "$VENV_DIR" && \
    "$VENV_DIR/bin/python" "$SETUP_SCRIPT" --venv-dir "$VENV_DIR" --model-dir "$MODEL_DIR"; then
    if [ "$had_previous_venv" -eq 1 ]; then
      rm -rf -- "$backup_venv"
    fi
    cleanup_runtime_stage
    return 0
  fi
  rm -rf -- "$VENV_DIR"
  if [ "$had_previous_venv" -eq 1 ]; then
    mv "$backup_venv" "$VENV_DIR"
  fi
  echo "The bundled runtime failed verification after installation." >&2
  return 1
}

finish_installation() {
  "$VENV_DIR/bin/python" "$MIGRATION_SCRIPT"
  if [ "$MODE" = "--install" ]; then
    wait_for_openalex_setup
  fi
}

if [ "$MODE" = "--install" ]; then
  sh "$OPENALEX_SETUP_SCRIPT" &
  OPENALEX_SETUP_PID=$!
fi

if find_bundled_runtime; then
  install_bundled_runtime "$BUNDLED_RUNTIME"
  finish_installation
  echo "AreaDay is ready. The bundled runtime was verified without downloading dependencies."
  exit 0
else
  runtime_lookup_status=$?
  if [ "$runtime_lookup_status" -ne 1 ]; then
    exit "$runtime_lookup_status"
  fi
  if [ "$MODE" = "--runtime-only" ]; then
    echo "No bundled runtime was found for $PLATFORM_ID." >&2
    exit 1
  fi
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
  finish_installation
  echo "Installation verified; removing the disposable package-download cache..."
  if ! "$UV_BIN" cache clean --no-config; then
    echo "Warning: installation succeeded, but the disposable uv cache could not be cleaned." >&2
  fi
  exit 0
else
  exit $?
fi
