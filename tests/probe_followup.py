"""Follow-up probe: nail down the exact workable forms after probe_turingdb_syntax.

Answers the questions that decide the architecture:
  1. Does an UNTYPED quantified traversal work when seeded? (-> Q2 is possible)
  2. Do IS NULL / IS NOT NULL work when the property exists on some nodes?
     (COD is full of missing fields, so this drives the ingest null strategy.)
  3. Do property indexes work, and do they speed up seeded lookups?
  4. Does LOAD JSONL accept the documented APOC shape?
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from turingdb import TuringDB, TuringDBException

HOST = os.environ.get("TURING_HOST", "http://localhost:6691")
GRAPH = os.environ.get("PROBE_GRAPH", "probe_critical")
TURING_DIR = os.path.expanduser(os.environ.get("TURING_DIR", "~/ccdc_demo/turing-data-137"))


def show(client: TuringDB, label: str, cypher: str) -> None:
    try:
        df = client.query(cypher)
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL  {label}\n          {' '.join(str(exc).split())[:200]}")
    else:
        val = ""
        try:
            if len(df) and len(df.columns) == 1:
                val = f" = {df.iloc[0, 0]}"
            elif len(df):
                val = f" first={list(df.iloc[0])[:5]}"
        except Exception:  # noqa: BLE001
            pass
        print(f"  PASS  {label}  rows={len(df)}{val}")


def main() -> int:
    client = TuringDB(host=HOST)
    try:
        client.query(f"LOAD GRAPH {GRAPH}")
    except Exception:  # noqa: BLE001
        pass
    client.set_graph(GRAPH)

    print("=== 1. UNTYPED quantified traversal (the Q2 candidate) ===")
    show(client, "seeded untyped {1,8}",
         "MATCH (a:Atom {uid:'A0'})-[e]->{1,8}(b:Atom) RETURN count(b)")
    show(client, "seeded untyped {1,1}",
         "MATCH (a:Atom {uid:'A0'})-[e]->{1,1}(b:Atom) RETURN count(b)")
    show(client, "seeded untyped {1,4}",
         "MATCH (a:Atom {uid:'A0'})-[e]->{1,4}(b:Atom) RETURN count(b)")
    show(client, "seeded untyped {8,8} (exact depth)",
         "MATCH (a:Atom {uid:'A0'})-[e]->{8,8}(b:Atom) RETURN count(b)")
    show(client, "seeded untyped {1,12}",
         "MATCH (a:Atom {uid:'A0'})-[e]->{1,12}(b:Atom) RETURN count(b)")
    show(client, "untyped {1,8} returning endpoints",
         "MATCH (a:Atom {uid:'A0'})-[e]->{1,8}(b:Atom) RETURN b.uid LIMIT 10")
    show(client, "untyped one-or-more +",
         "MATCH (a:Atom {uid:'A0'})-[e]->+(b:Atom) RETURN count(b)")
    show(client, "untyped, seed by internal id",
         "MATCH (a)-[e]->{1,8}(b:Atom) WHERE a = 0 RETURN count(b)")
    show(client, "untyped {1,8} + WHERE on endpoint prop",
         "MATCH (a:Atom {uid:'A0'})-[e]->{1,8}(b:Atom) WHERE b.element = 'O' RETURN count(b)")

    print("\n=== 2. NULL handling (COD has many missing fields) ===")
    # temperature exists on both Structures; add a node missing it to test.
    try:
        client.new_change()
        client.query(
            "CREATE (:Structure {cod_id:1000003, r_factor:0.09, formula:'C1 H4', "
            "hm_symbol:'P 1'})"
        )
        client.query("CHANGE SUBMIT")
        client.checkout()
        print("  (added Structure 1000003 with NO temperature property)")
    except Exception as exc:  # noqa: BLE001
        print(f"  could not add test node: {' '.join(str(exc).split())[:150]}")

    show(client, "IS NOT NULL on a partially-present prop",
         "MATCH (s:Structure) WHERE s.temperature IS NOT NULL RETURN s.cod_id")
    show(client, "IS NULL on a partially-present prop",
         "MATCH (s:Structure) WHERE s.temperature IS NULL RETURN s.cod_id")
    show(client, "plain projection of a missing prop",
         "MATCH (s:Structure) RETURN s.cod_id, s.temperature")
    show(client, "comparison against a missing prop",
         "MATCH (s:Structure) WHERE s.temperature < 200.0 RETURN s.cod_id")
    show(client, "sentinel approach: has_temperature flag",
         "MATCH (s:Structure) WHERE s.r_factor > 0.0 RETURN count(s)")

    print("\n=== 3. Property indexes ===")
    try:
        client.new_change()
        client.query("CREATE INDEX uid_index FOR (n) ON n.uid")
        client.query("CHANGE SUBMIT")
        client.checkout()
        print("  created index uid_index")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL creating index: {' '.join(str(exc).split())[:200]}")
    show(client, "db.showIndexes()", "CALL db.showIndexes()")
    show(client, "seeded lookup via indexed prop",
         "MATCH (a:Atom {uid:'A5'}) RETURN a.element")

    print("\n=== 4. LOAD JSONL (documented APOC shape) ===")
    data_dir = os.path.join(TURING_DIR, "data")
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "probe_jsonl.jsonl")
    lines = [
        {"type": "node", "id": "0", "labels": ["Atom"],
         "properties": {"uid": "J0", "element": "O", "occupancy": 1.0}},
        {"type": "node", "id": "1", "labels": ["Atom"],
         "properties": {"uid": "J1", "element": "N", "occupancy": 1.0}},
        {"type": "node", "id": "2", "labels": ["Structure"],
         "properties": {"cod_id": 2000001, "formula": "C2 H6"}},
        {"type": "relationship", "id": "0", "label": "CONTACT",
         "start": {"id": "0"}, "end": {"id": "1"},
         "properties": {"kind": "hbond", "length": 2.11, "symop": "x,y,z"}},
        {"type": "relationship", "id": "1", "label": "HAS_SITE",
         "start": {"id": "2"}, "end": {"id": "0"}, "properties": {}},
    ]
    with open(path, "w") as fh:
        for obj in lines:
            fh.write(json.dumps(obj) + "\n")
    print(f"  wrote {path}")

    for stmt in [
        "LOAD JSONL 'probe_jsonl.jsonl' AS probe_jsonl_graph",
    ]:
        try:
            client.query(stmt)
            print(f"  PASS  {stmt}")
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  {stmt}\n          {' '.join(str(exc).split())[:250]}")

    try:
        client.set_graph("probe_jsonl_graph")
        show(client, "loaded graph node count", "MATCH (n) RETURN count(n)")
        show(client, "loaded graph edge count", "MATCH ()-[r]->() RETURN count(r)")
        show(client, "loaded props survived", "MATCH (a:Atom) RETURN a.uid, a.element")
        show(client, "loaded edge props survived",
             "MATCH ()-[r:CONTACT]->() RETURN r.kind, r.length, r.symop")
        show(client, "labels round-tripped", "CALL db.labels()")
        show(client, "edge types round-tripped", "CALL db.edgeTypes()")
    except Exception as exc:  # noqa: BLE001
        print(f"  could not query loaded graph: {' '.join(str(exc).split())[:200]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
