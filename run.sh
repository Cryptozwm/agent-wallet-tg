#!/usr/bin/env bash
# Keeps one bot instance running; restarts it 10 s after it exits. Log: ~/.config/agent-wallet-tg/bot.log
CONF="$HOME/.config/agent-wallet-tg"
mkdir -p "$CONF"
if ! grep -qE '^TG_BOT_TOKEN=[^[:space:]]+' "$CONF/env" 2>/dev/null; then
  echo "TG_BOT_TOKEN is missing in $CONF/env (copy env.example there and fill it in)"
  exit 1
fi
command -v baw >/dev/null || echo "warning: baw not found on PATH (npm install -g @binance/agentic-wallet), or set BAW_BIN in the env file"
if command -v flock >/dev/null; then
  exec 9>"$CONF/run.lock"
  flock -n 9 || { echo "bot is already running"; exit 0; }
elif [ -f "$CONF/run.pid" ] && kill -0 "$(cat "$CONF/run.pid")" 2>/dev/null; then  # macOS has no flock
  echo "bot is already running"; exit 0
fi
echo $$ > "$CONF/run.pid"
cd "$(dirname "$0")"
echo "bot running (Ctrl+C to stop). Logs: $CONF/bot.log and $CONF/stdout.log"
while true; do
  python3 -u bot.py >>"$CONF/stdout.log" 2>&1
  sleep 10
done
