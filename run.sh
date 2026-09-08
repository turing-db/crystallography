#!/usr/bin/env bash
# One-command setup for the CCDC crystallography demo.
#
#   ./run.sh            start TuringDB + the visualizer (assumes data is built)
#   ./run.sh --ingest   also acquire COD and build the graphs from scratch
#
# The ingest path takes a while and needs network access to
# crystallography.net (rsync for CIFs, MySQL for metadata).
set -euo pipefail
cd "$(dirname "$0")"

TURING_PORT="${TURING_PORT:-6691}"
UI_PORT="${UI_PORT:-8087}"
TURING_DIR="${TURING_DIR:-$PWD/turing-data}"

command -v uv >/dev/null || {
  echo "uv is required: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
}

echo "==> python deps"
uv sync --extra dev

BIN="$(uv run python -c 'import turingdb,os;print(os.path.join(os.path.dirname(turingdb.__file__),"bin","turingdb"))')"
echo "==> turingdb binary: $BIN"

mkdir -p "$TURING_DIR"
if ! (echo > "/dev/tcp/127.0.0.1/$TURING_PORT") >/dev/null 2>&1; then
  echo "==> starting TuringDB on :$TURING_PORT"
  "$BIN" start -turing-dir "$TURING_DIR" -p "$TURING_PORT" -demon \
      > turingdb.log 2>&1
  sleep 6
else
  echo "==> TuringDB already listening on :$TURING_PORT"
fi

if [[ "${1:-}" == "--ingest" ]]; then
  echo "==> acquiring COD (CIFs by rsync, metadata from the public MySQL mirror)"
  uv run python -m ingest.download --dataset slice
  echo "==> building the graph"
  uv run python -m ingest.build_graph --dataset slice --limit 10000
  uv run python -m ingest.project_contacts
  echo "==> loading into TuringDB"
  TURING_HOST="http://localhost:$TURING_PORT" \
    uv run python -m ingest.load_turingdb \
      --jsonl data/jsonl/cod_slice.jsonl --graph cod_slice_v2 \
      --host "http://localhost:$TURING_PORT" --turing-dir "$TURING_DIR"
  echo "==> building the chronologically-committed corpus (one commit per year)"
  uv run python -m ingest.build_versioned \
      --host "http://localhost:$TURING_PORT" --per-year 0 --fresh
fi

echo "==> frontend"
cd visualizer
[ -d node_modules ] || npm install
npm run build
echo "==> serving on :$UI_PORT (proxying /api -> :$TURING_PORT)"
TURING_FRONTEND_PORT="$UI_PORT" TURING_API_PORT="$TURING_PORT" node server.js
