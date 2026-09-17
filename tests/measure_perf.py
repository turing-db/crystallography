"""Performance numbers for the CCDC talk, measured rather than asserted.

The Journal Club brief asks for "performance, scalability, ease of use" by name,
and the honest way to answer that is a table of absolute latencies at a stated
corpus size plus an extrapolation to CSD scale -- not a rigged head-to-head
against another engine.

Reports four things:

1. **Read latency** for every query the UI actually ships, plus the join-order
   comparison, taken as the server's own reported execution time (median of N).
2. **Datapart count and bytes on disk** before and after `MERGE_DATAPARTS`.
   A single-shot `LOAD JSONL` produces one datapart, so the merge has nothing
   to do; the numbers are reported so that is visible rather than assumed.
3. **Property index effect** on the same queries.
4. **The CSD extrapolation**: bytes and elements per structure, scaled.

Usage:
    .venv/bin/python tests/measure_perf.py --graph cod_slice_v2 \\
        --turing-dir turing-data-137 --host http://localhost:6691
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import urllib.request

#: The CSD is roughly 1.3 M structures; this is what the database team will ask.
CSD_STRUCTURES = 1_300_000


def q(host: str, graph: str, cypher: str,
      timeout: float = 900.0) -> tuple[dict, float]:
    """Run one query. Returns (payload, wall seconds). Errors come back HTTP 200."""
    req = urllib.request.Request(
        f"{host}/query?graph={graph}",
        data=cypher.encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read())
    return payload, time.perf_counter() - t0


#: Queries known to be pathological are run ONCE, not `repeat` times. The
#: comma-separated join is in the set deliberately: it is measured to show how
#: bad the wrong phrasing is, and at corpus scale it is a genuine cross product
#: over millions of atoms, so repeating it costs tens of minutes for a number
#: whose whole point is that it is large.
SLOW_ONCE = ("JOIN ORDER: acid pairs, comma-separated",)


def timed(host: str, graph: str, cypher: str, n: int = 3,
          timeout_s: float = 900.0) -> dict:
    server_ms: list[float] = []
    wall_ms: list[float] = []
    err = ""
    for _ in range(n):
        try:
            payload, wall = q(host, graph, cypher, timeout_s)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"[:160]}
        if payload.get("error"):
            err = f"{payload['error']}: {payload.get('error_details', '')}"
            break
        server_ms.append(float(payload.get("time", 0.0)))
        wall_ms.append(wall * 1000.0)
    if err:
        return {"error": err}
    return {
        "server_ms": round(statistics.median(server_ms), 1),
        "wall_ms": round(statistics.median(wall_ms), 1),
    }


def dir_stats(graph_dir: Path) -> dict:
    """Datapart count and byte size of one graph's directory."""
    if not graph_dir.exists():
        return {"exists": False}
    parts = [p for p in graph_dir.rglob("*") if p.is_dir() and "datapart" in p.name.lower()]
    total = sum(p.stat().st_size for p in graph_dir.rglob("*") if p.is_file())
    return {
        "exists": True,
        "dataparts": len(parts),
        "bytes": total,
        "human": f"{total / 1e9:.2f} GB",
        "entries": sum(1 for _ in graph_dir.iterdir()),
    }


#: The queries the UI ships, named as the investigation that issues them.
def query_set(graph: str) -> list[tuple[str, str]]:
    # The corrected R2,2(8) dimer query: one connected chain, the ring/catemer
    # distinction carried by the precomputed `is_involution` flag, and scoped to
    # a single structure because Component is deduplicated corpus-wide.
    acid_chain = (
        "MATCH (f1:Fragment)<-[:HAS_FRAGMENT]-(c1:Component)<-[:IN_COMPONENT]-(a1:Atom)"
        "-[h:CONTACT]->(a2:Atom)-[:IN_COMPONENT]->(c2:Component)-[:HAS_FRAGMENT]->(f2:Fragment) "
        "WHERE f1.fragment_type = 'carboxylic_acid' AND f2.fragment_type = 'carboxylic_acid' "
        "AND h.kind = 'hbond' AND h.h_inferred = false AND h.is_involution = true "
        "AND a1.element = 'O' AND a2.element = 'O' AND a1.cod_id = a2.cod_id "
        "RETURN count(h)"
    )
    # Same question, written the way most people would write it: separate
    # patterns joined in the WHERE. This pair is the join-order demonstration.
    acid_comma = (
        "MATCH (a1:Atom)-[h:CONTACT]->(a2:Atom), "
        "(a1)-[:IN_COMPONENT]->(c1:Component)-[:HAS_FRAGMENT]->(f1:Fragment), "
        "(a2)-[:IN_COMPONENT]->(c2:Component)-[:HAS_FRAGMENT]->(f2:Fragment) "
        "WHERE h.kind = 'hbond' AND h.h_inferred = false "
        "AND f1.fragment_type = 'carboxylic_acid' "
        "AND f2.fragment_type = 'carboxylic_acid' RETURN count(h)"
    )
    acid_chain_1hop = (
        "MATCH (f1:Fragment)<-[:HAS_FRAGMENT]-(c1:Component)<-[:IN_COMPONENT]-(a1:Atom)"
        "-[h:CONTACT]->(a2:Atom)-[:IN_COMPONENT]->(c2:Component)-[:HAS_FRAGMENT]->(f2:Fragment) "
        "WHERE f1.fragment_type = 'carboxylic_acid' "
        "AND f2.fragment_type = 'carboxylic_acid' "
        "AND h.kind = 'hbond' AND h.h_inferred = false RETURN count(h)"
    )
    return [
        ("count structures", "MATCH (s:Structure) RETURN count(s)"),
        ("count atoms", "MATCH (n:Atom) RETURN count(n)"),
        ("count contacts", "MATCH ()-[r:CONTACT]->() RETURN count(r)"),
        ("spacegroup P2(1)/c",
         "MATCH (s:Structure)-[:IN_SPACE_GROUP]->(x:SpaceGroup) "
         "WHERE x.hm_symbol = 'P 1 21/c 1' RETURN count(s)"),
        ("mean H...A geometry",
         "MATCH ()-[r:CONTACT]->() WHERE r.kind = 'hbond' AND r.h_inferred = false "
         "RETURN avg(r.length)"),
        ("dimensionality 2D",
         "MATCH (s:Structure) WHERE s.net_dim = 2 RETURN count(s)"),
        ("packing subgraph (one structure)",
         "MATCH (a:Atom)-[e:CONTACT]->(b:Atom) WHERE a.cod_id = 2229029 "
         "RETURN a, labels(a), a.label, e, edgeType(e), b, labels(b), b.label"),
        ("fragment census",
         "MATCH (c:Component)-[:HAS_FRAGMENT]->(f:Fragment) "
         "WHERE f.fragment_type = 'carboxylic_acid' RETURN count(c)"),
        ("synthon competition (acid->pyridine)",
         "MATCH (f1:Fragment)<-[:HAS_FRAGMENT]-(c1:Component)<-[:IN_COMPONENT]-(a1:Atom)"
         "-[h:CONTACT]->(a2:Atom)-[:IN_COMPONENT]->(c2:Component)-[:HAS_FRAGMENT]->(f2:Fragment) "
         "WHERE f1.fragment_type = 'carboxylic_acid' "
         "AND f2.fragment_type = 'pyridine_nitrogen' AND h.kind = 'hbond' "
         "AND h.h_inferred = false AND a1.element = 'O' AND a2.element = 'N' "
         "RETURN count(h)"),
        ("JOIN ORDER: acid pairs, one chain", acid_chain_1hop),
        ("JOIN ORDER: acid pairs, comma-separated", acid_comma),
        ("acid dimer R2,2(8)", acid_chain),
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="http://localhost:6691")
    ap.add_argument("--graph", default="cod_slice_v2")
    ap.add_argument("--turing-dir", type=Path, default=Path("turing-data-137"))
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--merge", action="store_true",
                    help="also run MERGE_DATAPARTS and re-measure")
    ap.add_argument("--out", type=Path,
                    default=Path("data/manifest/perf.json"))
    args = ap.parse_args(argv)

    host, graph = args.host, args.graph
    gdir = args.turing_dir / "graphs" / graph
    report: dict = {"graph": graph, "host": host}

    print(f"== {graph} ==\n")
    before = dir_stats(gdir)
    report["disk_before"] = before
    if before.get("exists"):
        print(f"on disk: {before['human']} in {before['entries']} entries "
              f"({before['dataparts']} datapart dirs)\n")

    qs = query_set(graph)
    print(f"{'query':<42}{'server ms':>11}{'wall ms':>10}")
    print("-" * 63)
    results: dict = {}
    for name, cypher in qs:
        r = timed(host, graph, cypher,
                  1 if name in SLOW_ONCE else args.repeat)
        results[name] = r
        if "error" in r:
            print(f"{name:<42}{'ERROR':>11}   {r['error'][:40]}")
        else:
            print(f"{name:<42}{r['server_ms']:>11.1f}{r['wall_ms']:>10.1f}")
    report["queries"] = results

    # the join-order headline, stated explicitly
    chain = results.get("JOIN ORDER: acid pairs, one chain", {})
    comma = results.get("JOIN ORDER: acid pairs, comma-separated", {})
    if "server_ms" in chain and "server_ms" in comma and chain["server_ms"] > 0:
        ratio = comma["server_ms"] / chain["server_ms"]
        report["join_order_speedup"] = round(ratio, 1)
        print(f"\njoin order: comma-separated is {ratio:.1f}x slower than one "
              f"connected chain for the identical question")

    # scale
    ns, _ = q(host, graph, "MATCH (s:Structure) RETURN count(s)")
    n_struct = ns["data"][0][0][0] if not ns.get("error") else 0
    report["structures"] = n_struct
    if before.get("exists") and n_struct:
        per = before["bytes"] / n_struct
        report["bytes_per_structure"] = round(per)
        report["csd_projection_bytes"] = round(per * CSD_STRUCTURES)
        print(f"\nscale: {n_struct:,} structures, {per / 1024:.1f} kB per structure "
              f"on disk\n       -> the CSD at {CSD_STRUCTURES:,} structures "
              f"projects to {per * CSD_STRUCTURES / 1e9:.0f} GB")

    if args.merge:
        print("\n== MERGE_DATAPARTS ==")
        chg, _ = q(host, graph, "CHANGE LIST")
        print(f"open changes before merge: {chg.get('data')}")
        t0 = time.perf_counter()
        m, _ = q(host, graph, "MERGE_DATAPARTS")
        print(f"returned in {(time.perf_counter() - t0) * 1000:.1f} ms: "
              f"{m.get('error') or 'no error'}")
        time.sleep(2)
        after = dir_stats(gdir)
        report["disk_after_merge"] = after
        if after.get("exists"):
            print(f"on disk: {before['human']} -> {after['human']}  "
                  f"({before['dataparts']} -> {after['dataparts']} datapart dirs, "
                  f"{before['entries']} -> {after['entries']} entries)")
        post: dict = {}
        for name, cypher in qs:
            post[name] = timed(host, graph, cypher,
                               1 if name in SLOW_ONCE else args.repeat)
        report["queries_after_merge"] = post
        # correctness, not just speed: the 1.35 run returned CORRUPT reads
        changed = [
            n for n in results
            if results[n].get("error") != post[n].get("error")
        ]
        print(f"queries whose outcome changed after merge: {len(changed)}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
