#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SKILL_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
HELP_PATH="$SKILL_DIR/assets/openalex-help.html"
CONFIG_DIR="$HOME/.researchramp"
CONFIG_PATH="$CONFIG_DIR/credentials.ini"
LAST_ATTEMPTED_VALUE=""

cleanup() {
  LAST_ATTEMPTED_VALUE=""
}
trap cleanup EXIT
trap 'cleanup; exit 130' HUP INT TERM

if [ ! -f "$HELP_PATH" ]; then
  echo "OpenAlex help page is missing: $HELP_PATH" >&2
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  echo "OpenAlex setup requires the operating system's curl command." >&2
  exit 1
fi

read_api_key() {
  sed -n 's/^[[:space:]]*api_key[[:space:]]*=[[:space:]]*//p' "$CONFIG_PATH" \
    | sed -n '1{s/[[:space:]]*$//;p;}'
}

create_config_template() {
  mkdir -p "$CONFIG_DIR"
  chmod 700 "$CONFIG_DIR"
  if [ ! -f "$CONFIG_PATH" ]; then
    umask 077
    temporary=$(mktemp "$CONFIG_DIR/.credentials.XXXXXX")
    {
      printf '[openalex]\n\n'
      printf '# 请把完整的 OpenAlex API Key 粘贴到等号右侧，然后保存。\n'
      printf '# 不要把本文件上传或发送到聊天中。\n'
      printf '# 如果明确选择匿名额度，请填写 anonymous。\n'
      printf 'api_key =\n'
    } >"$temporary"
    chmod 600 "$temporary"
    mv "$temporary" "$CONFIG_PATH"
  else
    chmod 600 "$CONFIG_PATH"
  fi
}

open_setup_files() {
  case "$(uname -s)" in
    Darwin)
      open "$HELP_PATH"
      open -e "$CONFIG_PATH"
      ;;
    Linux)
      if ! command -v xdg-open >/dev/null 2>&1; then
        echo "OpenAlex setup requires the operating system's xdg-open command." >&2
        exit 1
      fi
      xdg-open "$HELP_PATH"
      xdg-open "$CONFIG_PATH"
      ;;
    *)
      echo "Use configure_openalex.ps1 on Windows." >&2
      exit 1
      ;;
  esac
}

validate_key() {
  api_key="$1"
  validation_body="$CONFIG_DIR/.openalex-validation-response"
  status=$(
    {
      printf 'url = "https://api.openalex.org/rate-limit"\n'
      printf 'header = "Authorization: Bearer %s"\n' "$api_key"
      printf 'connect-timeout = 15\n'
      printf 'max-time = 30\n'
      printf 'silent\nshow-error\n'
    } | curl --config - --output "$validation_body" --write-out '%{http_code}' 2>/dev/null || true
  )
  rm -f "$validation_body"
  case "$status" in
    200) return 0 ;;
    401|403) return 2 ;;
    *) return 3 ;;
  esac
}

create_config_template
SETUP_FILES_OPENED=0
if [ -z "$(read_api_key)" ]; then
  open_setup_files
  SETUP_FILES_OPENED=1
  echo "OpenAlex instructions and the local configuration file are open."
  echo "Look for credentials.ini in the Codex/WorkBuddy file panel or the system text editor."
  echo "Paste the key after 'api_key =', save the file, and keep this task open until validation finishes."
fi

while :; do
  api_key=$(read_api_key)
  if [ -z "$api_key" ] || [ "$api_key" = "$LAST_ATTEMPTED_VALUE" ]; then
    sleep 0.5
    continue
  fi
  LAST_ATTEMPTED_VALUE="$api_key"

  if [ "$api_key" = "anonymous" ]; then
    echo "OpenAlex anonymous access selected at $CONFIG_PATH"
    exit 0
  fi
  case "$api_key" in
    *[!A-Za-z0-9_-]*)
      echo "OpenAlex did not recognize the saved value. Replace it with the complete API key and save again." >&2
      if [ "$SETUP_FILES_OPENED" -eq 0 ]; then
        open_setup_files
        SETUP_FILES_OPENED=1
      fi
      continue
      ;;
  esac
  if [ "${#api_key}" -lt 12 ] || [ "${#api_key}" -gt 200 ]; then
    echo "OpenAlex did not recognize the saved value. Replace it with the complete API key and save again." >&2
    if [ "$SETUP_FILES_OPENED" -eq 0 ]; then
      open_setup_files
      SETUP_FILES_OPENED=1
    fi
    continue
  fi

  if validate_key "$api_key"; then
    echo "OpenAlex key verified at $CONFIG_PATH"
    exit 0
  else
    validation_result=$?
    if [ "$validation_result" -eq 2 ]; then
      echo "OpenAlex did not recognize this key. Copy the complete key from OpenAlex Settings and save again." >&2
      if [ "$SETUP_FILES_OPENED" -eq 0 ]; then
        open_setup_files
        SETUP_FILES_OPENED=1
      fi
      continue
    fi
    echo "Could not connect to OpenAlex to verify the key. Run this setup again when OpenAlex is reachable." >&2
    exit 1
  fi
done
