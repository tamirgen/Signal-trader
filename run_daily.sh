#!/usr/bin/env bash
# Daily pipeline. Every day: fetch new episodes -> signals.json. On US trading days also:
# entries and exits. Then the activity report and the dashboard.
# Pass --paper to place orders; without it, trade and exits are dry runs.
set -euo pipefail
cd "$(dirname "$0")"
rm -f reports/planned_orders.csv reports/exits.csv reports/fetch_log.json

python fetch.py
python themes.py || echo "theme step failed; continuing"
python ingest.py

rc=0
python common.py --is-trading-day || rc=$?
if [ "$rc" -eq 0 ]; then
  export MARKET_STATUS=open
  python trade.py "$@"
  python exits.py "$@"
elif [ "$rc" -eq 3 ]; then
  export MARKET_STATUS=closed
  echo "Market closed today; no trading."
else
  echo "Trading-day check failed (exit $rc); see the error above." >&2
  exit "$rc"
fi

python activity.py
python monitor.py
