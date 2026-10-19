#!/usr/bin/env bash
# Gonets install: venv, dependencies, .env, hook settings, optional systemd unit.
#   ./install.sh             first run: creates .env and stops; second run: checks the config
#   ./install.sh --systemd   also installs and starts the systemd user service
set -euo pipefail
cd "$(dirname "$0")"
REPO="$(pwd -P)"
PY="${PYTHON:-python3}"

if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv
fi
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -e '.[voice]'

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created .env. Fill in TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_IDS and CLAUDE_BIN"
  echo "(absolute path, see: command -v claude), then run ./install.sh again."
  exit 0
fi

render() {  # render <template> <target>: substitute the absolute repository path
  sed "s#@REPO@#${REPO}#g" "$1" > "$2"
}

if [ ! -f claude-hook-settings.json ]; then
  render claude-hook-settings.example.json claude-hook-settings.json
  echo "Rendered claude-hook-settings.json for ${REPO}"
fi

.venv/bin/claude-tg-bot --check

if [ "${1:-}" = "--systemd" ]; then
  unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "$unit_dir"
  for unit in claude-tg-bot.service claude-tg-bot-down.service; do
    render "systemd/${unit}" "${unit_dir}/${unit}"
  done
  systemctl --user daemon-reload
  systemctl --user enable --now claude-tg-bot.service
  echo "Started. Logs: journalctl --user -u claude-tg-bot -f"
  echo "To keep it running after logout: loginctl enable-linger ${USER}"
else
  echo "Config OK. Run in the foreground: .venv/bin/claude-tg-bot"
  echo "Or install the systemd user service: ./install.sh --systemd"
fi
