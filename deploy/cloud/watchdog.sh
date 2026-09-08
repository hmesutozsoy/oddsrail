#!/bin/sh
# Every five minutes: if the app does not answer, or its scheduler heartbeat
# is older than 15 minutes, restart the service and say so in the journal.
set -u
OUT=$(curl -s -m 10 http://127.0.0.1:8787/healthz || true)
case "$OUT" in
  *'"ok":true'*) ;;
  *) echo "oddsrail-cloud watchdog: no healthy answer ($OUT); restarting"; systemctl restart oddsrail-cloud; exit 0 ;;
esac
AGE=$(printf '%s' "$OUT" | sed -n 's/.*"scheduler_tick_age_s":\([0-9.]*\).*/\1/p')
if [ -n "$AGE" ] && [ "${AGE%.*}" -gt 900 ]; then
  echo "oddsrail-cloud watchdog: scheduler heartbeat is ${AGE}s old; restarting"
  systemctl restart oddsrail-cloud
fi
