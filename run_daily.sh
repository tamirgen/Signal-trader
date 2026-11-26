#!/usr/bin/env bash
# Daily pipeline: ingest -> entries -> exits -> report. Runs only on US trading days.
# Pass --paper to place orders; without it, trade and exits are dry runs.
set -euo pipefail
cd "$(dirname "$0")"
rc=0
python common.py --is-trading-day || rc=$?
if [ "$rc" -eq 3 ]; then
  echo "Market closed today; nothing to do."
  exit 0
elif [ "$rc" -ne 0 ]; then
  echo "Trading-day check failed (exit $rc); see the error above." >&2
  exit "$rc"
fi
python ingest.py
python trade.py "$@"
python exits.py "$@"
python monitor.py
