"""Decide which ALREADY-BUILT turingdb binary this demo should run on.

Two candidates are already on the box (no new build):
  A. ~/turingdb-src/build/turing_install/bin/turingdb  (source build 3c113474b,
     2026-07-16) - the one proj2 and def_demo drive the visualizer with.
  B. .venv/.../turingdb/bin/turingdb  (v1.37 wheel, 2026-08-21).

The demo is being grafted onto the visualizer, so the binary must satisfy BOTH:
  1. the visualizer's data layer  -> db.getNodes / db.getNodeEdges / db.getEdges
  2. Q2's deep traversal          -> postfix quantifiers  -[e]->{1,8}

Run against each candidate and compare.
"""

from __future__ import annotations

import os
import sys

from turingdb import TuringDB

HOST = os.environ.get("TURING_HOST", "http://localhost:6691")
GRAPH = os.environ.get("PROBE_GRAPH", "probe_critical")

CHECKS: list[tuple[str, str]] = [
    # --- visualizer data layer -------------------------------------------
    ("db.getNodes(['Atom'])", "CALL db.getNodes(['Atom'])"),
    ("db.getNodes single label", "CALL db.getNodes('Atom')"),
    ("db.getNodes with limit", "CALL db.getNodes(['Atom'], 10)"),
    ("db.getNodeEdges", "CALL db.getNodeEdges([0], 10, [], [], [], [], false)"),
    ("db.getEdges", "CALL db.getEdges(['CONTACT'])"),
    ("db.listNodes", "CALL db.listNodes()"),
    ("LIST AVAILABLE GRAPHS", "LIST AVAILABLE GRAPHS"),
    ("LIST GRAPH", "LIST GRAPH"),
    # --- Q2 traversal ------------------------------------------------------
    ("quantifier {1,8}", "MATCH (a:Atom {uid:'A0'})-[e]->{1,8}(b:Atom) RETURN count(b)"),
    ("quantifier {8,8}", "MATCH (a:Atom {uid:'A0'})-[e]->{8,8}(b:Atom) RETURN count(b)"),
    ("quantifier +", "MATCH (a:Atom {uid:'A0'})-[e]->+(b:Atom) RETURN count(b)"),
    # --- versioning --------------------------------------------------------
    ("CALL db.history()", "CALL db.history()"),
    ("CALL db.showIndexes()", "CALL db.showIndexes()"),
]


def main() -> int:
    print(f"--- {HOST} ---")
    client = TuringDB(host=HOST)
    try:
        client.query(f"LOAD GRAPH {GRAPH}")
    except Exception:  # noqa: BLE001
        pass
    client.set_graph(GRAPH)

    for label, cypher in CHECKS:
        try:
            df = client.query(cypher)
        except Exception as exc:  # noqa: BLE001
            msg = " ".join(str(exc).split())
            # a dead socket here means the statement killed the server
            dead = "Connection refused" in msg or "peer closed" in msg or "incomplete" in msg
            tag = "CRASH" if dead else "FAIL "
            print(f"  {tag} {label:<26} {msg[:95]}")
            if dead:
                print("         !! server appears to have died - aborting")
                return 1
        else:
            print(f"  PASS  {label:<26} rows={len(df)} cols={list(df.columns)[:4]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
