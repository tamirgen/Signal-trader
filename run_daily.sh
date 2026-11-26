#!/usr/bin/env bash
# Daily pipeline: ingest -> entries -> exits -> report. Runs only on US trading days.
# Pass --paper to place orders; without it, trade and exits are dry runs.
set -euo pipefail
cd "$(dirname "$0")"
if ! python common.py --is-trading-day; then
  echo "Market closed today; nothing to do."
  exit 0
fi
python ingest.py
python trade.py "$@"
python exits.py "$@"
python monitor.py
