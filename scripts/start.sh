#!/usr/bin/env bash
# Run the API and the job worker(s) in one container. Railway (and most PaaS) inject $PORT; the app
# reads SEMSHEET_PORT. If either process exits, the container exits so the platform restarts it.
set -euo pipefail

cd "$(dirname "$0")/../server"

export SEMSHEET_PORT="${PORT:-${SEMSHEET_PORT:-8000}}"
export SEMSHEET_DATA_DIR="${SEMSHEET_DATA_DIR:-/app/data}"
WORKERS="${SEMSHEET_WORKERS:-1}"

pids=()
for _ in $(seq 1 "$WORKERS"); do
  python -m semsheet.worker &
  pids+=("$!")
done
python -m semsheet.main &
pids+=("$!")

shutdown() {
  kill -TERM "${pids[@]}" 2>/dev/null || true
}
trap shutdown TERM INT

set +e
wait -n "${pids[@]}"
status=$?
set -e
shutdown
wait || true
exit "$status"
