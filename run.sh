#!/usr/bin/env bash
# One-command setup.
#
#   ./run.sh            start TuringDB + the browser studio on existing data
#   ./run.sh --ingest   also acquire COD and build the graphs from scratch
#
# The ingest path takes roughly 25 minutes and needs network access to
# crystallography.net (rsync for CIFs, MySQL for metadata).
#
# Once running: http://localhost:8087 for the studio, `crystal facts` for the
# command line. See TUTORIAL.md.
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
  # --out is explicit: build_graph would otherwise emit data/jsonl/slice.jsonl
  # while load_turingdb below reads cod_slice.jsonl, and the mismatch only
  # shows up as a missing file at the end of a long ingest.
  uv run python -m ingest.build_graph --dataset slice \
      --out data/jsonl/cod_slice.jsonl
  uv run python -m ingest.project_contacts \
      --in data/jsonl/cod_slice.jsonl --out data/jsonl/cod_contacts_v2.jsonl
  echo "==> loading into TuringDB"
  TURING_HOST="http://localhost:$TURING_PORT" \
    uv run python -m ingest.load_turingdb \
      --jsonl data/jsonl/cod_slice.jsonl --graph cod_slice_v2 \
      --host "http://localhost:$TURING_PORT" --turing-dir "$TURING_DIR"
  TURING_HOST="http://localhost:$TURING_PORT" \
    uv run python -m ingest.load_turingdb \
      --jsonl data/jsonl/cod_contacts_v2.jsonl --graph cod_contacts_v2 \
      --host "http://localhost:$TURING_PORT" --turing-dir "$TURING_DIR"
  echo "==> building the chronologically-committed corpus (one commit per year)"
  uv run python -m ingest.build_versioned \
      --host "http://localhost:$TURING_PORT" --per-year 0 --fresh
fi

# A graph that exists on disk is NOT resident after a server start, and an
# unloaded graph answers GRAPH_NOT_FOUND rather than returning nothing -- which
# reads as "the demo is broken" rather than "load it first". Load whatever is
# there, and say plainly when there is nothing.
echo "==> loading graphs"
uv run python - "$TURING_PORT" <<'PY'
import sys
from turingdb import TuringDB

host = f"http://localhost:{sys.argv[1]}"
c = TuringDB(host=host)
try:
    available = set(c.list_available_graphs()["graphName"])
except Exception as exc:
    print(f"    cannot reach TuringDB at {host}: {exc}")
    raise SystemExit(1)

wanted = [g for g in ("cod_slice_v2", "cod_contacts_v2",
                      "cod_versioned", "cod_versions") if g in available]
if not wanted:
    print("    no crystallography graphs on this server yet.")
    print("    Run  ./run.sh --ingest  to acquire COD and build them")
    print("    (~2.8 GB of CIFs, roughly 25 minutes).")
    raise SystemExit(0)

for g in wanted:
    try:
        c.load_graph(g)
        print(f"    loaded {g}")
    except Exception as exc:
        # already loaded is the common, harmless case
        print(f"    {g}: {str(exc)[:80]}")
PY

echo "==> frontend"
cd visualizer
[ -d node_modules ] || npm install
npm run build
echo "==> studio on http://localhost:$UI_PORT  (proxying /api -> :$TURING_PORT)"
echo "==> command line: TURING_HOST=http://localhost:$TURING_PORT uv run python crystal.py facts"
TURING_FRONTEND_PORT="$UI_PORT" TURING_API_PORT="$TURING_PORT" node server.js
