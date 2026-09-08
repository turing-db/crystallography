"""Load the generated JSONL into TuringDB and verify what actually landed.

`LOAD JSONL '<file>' AS <graph>` reads from `<turing-dir>/data/`, so the file is
copied there first. The load itself is a single statement; the value of this
module is the verification afterwards -- node and edge counts per label, and a
handful of chemistry spot-checks -- because a graph that loaded without error
but lost a property type is worse than one that failed loudly.

Usage:
    uv run python -m ingest.load_turingdb --jsonl data/jsonl/cod_slice.jsonl \
        --graph cod_slice
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Sequence

from turingdb import TuringDB, TuringDBException

DEFAULT_HOST = "http://localhost:6691"
DEFAULT_TURING_DIR = Path.home() / "ccdc_demo" / "turing-data-137"


def load(
    client: TuringDB,
    jsonl: Path,
    graph: str,
    turing_dir: Path,
) -> float:
    """Copy the JSONL into the server's data dir and LOAD it. Returns seconds."""
    data_dir = turing_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    dest = data_dir / jsonl.name
    if dest.resolve() != jsonl.resolve():
        shutil.copy2(jsonl, dest)
    print(f"  staged {dest} ({dest.stat().st_size / 1e6:.1f} MB)")

    t0 = time.time()
    client.query(f"LOAD JSONL '{jsonl.name}' AS {graph}")
    return time.time() - t0


def verify(client: TuringDB, graph: str) -> dict[str, object]:
    """Report what is actually in the graph, and spot-check the chemistry."""
    client.set_graph(graph)
    out: dict[str, object] = {}

    total_n = int(client.query("MATCH (n) RETURN count(n)").iloc[0, 0])
    total_e = int(client.query("MATCH ()-[r]->() RETURN count(r)").iloc[0, 0])
    out["nodes"] = total_n
    out["edges"] = total_e
    print(f"\n  nodes {total_n:,}   edges {total_e:,}")

    labels = client.query("CALL db.labels()")
    per_label: dict[str, int] = {}
    print("\n  nodes by label:")
    for lab in labels.iloc[:, 1]:
        try:
            n = int(client.query(f"MATCH (n:{lab}) RETURN count(n)").iloc[0, 0])
        except TuringDBException:
            n = -1
        per_label[str(lab)] = n
        print(f"    {str(lab):<14} {n:>10,}")
    out["by_label"] = per_label

    etypes = client.query("CALL db.edgeTypes()")
    per_type: dict[str, int] = {}
    print("\n  edges by type:")
    for et in etypes.iloc[:, 1]:
        try:
            n = int(client.query(f"MATCH ()-[r:{et}]->() RETURN count(r)").iloc[0, 0])
        except TuringDBException:
            n = -1
        per_type[str(et)] = n
        print(f"    {str(et):<20} {n:>10,}")
    out["by_edge_type"] = per_type

    # ---- chemistry spot-checks ------------------------------------------
    print("\n  spot checks:")
    checks: list[tuple[str, str]] = [
        ("hydrogen bonds", "MATCH ()-[r:CONTACT]->() WHERE r.kind = 'hbond' RETURN count(r)"),
        ("  of which inferred",
         "MATCH ()-[r:CONTACT]->() WHERE r.h_inferred = true RETURN count(r)"),
        ("halogen bonds",
         "MATCH ()-[r:CONTACT]->() WHERE r.kind = 'halogen' RETURN count(r)"),
        ("mean H...A distance",
         "MATCH ()-[r:CONTACT]->() WHERE r.h_inferred = false RETURN avg(r.length)"),
        ("mean D-H...A angle",
         "MATCH ()-[r:CONTACT]->() WHERE r.h_inferred = false RETURN avg(r.angle)"),
        ("mean covalent bond length", "MATCH ()-[r:BONDED_TO]->() RETURN avg(r.length)"),
        ("components with an InChIKey",
         "MATCH (c:Component) WHERE c.has_inchikey = true RETURN count(c)"),
        ("solvent components",
         "MATCH (c:Component) WHERE c.is_solvent = true RETURN count(c)"),
        ("polymeric components",
         "MATCH (c:Component) WHERE c.is_polymeric = true RETURN count(c)"),
        ("carboxylic acid fragments",
         "MATCH (f:Fragment)<-[:HAS_FRAGMENT]-(c:Component) "
         "WHERE f.fragment_type = 'carboxylic_acid' RETURN count(c)"),
        ("structures with hydrogens",
         "MATCH (s:Structure) WHERE s.has_hydrogens = true RETURN count(s)"),
        ("structures with a measured temperature",
         "MATCH (s:Structure) WHERE s.has_temperature = true RETURN count(s)"),
    ]
    spot: dict[str, object] = {}
    for name, cypher in checks:
        try:
            v = client.query(cypher).iloc[0, 0]
            spot[name.strip()] = float(v)
            print(f"    {name:<34} {v:,.3f}" if isinstance(v, float)
                  else f"    {name:<34} {v:,}")
        except Exception as exc:  # noqa: BLE001
            spot[name.strip()] = f"FAILED: {exc}"
            print(f"    {name:<34} FAILED {' '.join(str(exc).split())[:80]}")
    out["spot_checks"] = spot

    # ---- the traversal the demo leans on --------------------------------
    print("\n  traversal check (untyped quantifier, the only form supported):")
    seed = client.query(
        "MATCH (a:Atom)-[e:CONTACT]->(b:Atom) RETURN a.uid LIMIT 1"
    )
    if len(seed):
        uid = seed.iloc[0, 0]
        for depth in (1, 2, 3, 4):
            try:
                q = (f"MATCH (a:Atom {{uid:'{uid}'}})-[e]->{{{depth},{depth}}}(b:Atom) "
                     f"RETURN count(b)")
                n = int(client.query(q).iloc[0, 0])
                ms = client.get_query_exec_time()
                print(f"    depth {depth}: {n:>8,} reached   {ms:.2f} ms")
            except Exception as exc:  # noqa: BLE001
                print(f"    depth {depth}: FAILED {' '.join(str(exc).split())[:90]}")
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--jsonl", type=Path, default=Path("data/jsonl/cod_slice.jsonl"))
    ap.add_argument("--graph", default="cod_slice")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--turing-dir", type=Path, default=DEFAULT_TURING_DIR)
    ap.add_argument("--skip-load", action="store_true", help="verify only")
    args = ap.parse_args(argv)

    if not args.skip_load and not args.jsonl.exists():
        print(f"missing {args.jsonl}; run ingest.build_graph first", file=sys.stderr)
        return 1

    client = TuringDB(host=args.host)
    print(f"connected to {args.host}")

    if not args.skip_load:
        # There is no DROP GRAPH, so a rebuild uses a fresh name or wipes in place.
        try:
            existing = client.query("LIST AVAILABLE GRAPHS")
            names = set(existing.iloc[:, 0].astype(str))
        except Exception:  # noqa: BLE001
            names = set()
        if args.graph in names:
            print(f"  graph '{args.graph}' already exists -- wiping it "
                  f"(TuringDB has no DROP GRAPH)")
            try:
                client.query(f"LOAD GRAPH {args.graph}")
            except Exception:  # noqa: BLE001
                pass
            client.set_graph(args.graph)
            client.new_change()
            client.query("MATCH (n) DETACH DELETE n")
            client.query("CHANGE SUBMIT")
            client.checkout()

        secs = load(client, args.jsonl, args.graph, args.turing_dir)
        print(f"  LOAD JSONL took {secs:.1f}s")

    summary = verify(client, args.graph)

    rep = Path("data/manifest") / f"{args.graph}_load_report.json"
    rep.parent.mkdir(parents=True, exist_ok=True)
    rep.write_text(json.dumps(summary, indent=2, default=str) + "\n")
    print(f"\n  report: {rep}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
