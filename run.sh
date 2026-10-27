#!/usr/bin/env bash
# Keeps one bot instance running; restarts it 10 s after it exits. Log: ~/.config/agent-wallet-tg/bot.log
CONF="$HOME/.config/agent-wallet-tg"
mkdir -p "$CONF"
exec 9>"$CONF/run.lock"
flock -n 9 || { echo "bot is already running"; exit 0; }
cd "$(dirname "$0")"
while true; do
  python3 -u bot.py >>"$CONF/stdout.log" 2>&1
  sleep 10
done
