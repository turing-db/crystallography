"""Hydrogen-bond network dimensionality, done the way a crystallographer would.

Why the naive traversal is wrong
--------------------------------
The contact edges we store form a *quotient graph*: the nodes are
asymmetric-unit sites and each edge carries the symmetry operation that
generated the neighbour. A hydrogen-bonded chain running through the crystal is
therefore NOT a long path in that graph -- it is a short cycle, because after a
few hops you come back to the same asymmetric-unit site one unit cell over.
Counting reachable nodes at increasing depth saturates almost immediately and
reports every structure as "isolated", which is exactly what it did.

The correct method
------------------
This is the standard periodic-net calculation. Build the labelled quotient graph
of the full unit cell, where every edge carries the integer lattice vector you
cross when you traverse it. Take a spanning tree, and for every non-tree edge
compute the net lattice translation around the cycle it closes. The **rank of
the set of those translation vectors** is the dimensionality of the net:

    rank 0  ->  0D  finite motif (dimer, ring, isolated cluster)
    rank 1  ->  1D  chain
    rank 2  ->  2D  sheet
    rank 3  ->  3D  framework

This is precisely what the `symop` on every edge is *for*: it lets us decide the
dimensionality of an infinite periodic network without ever materialising a
supercell. It is also a real crystallographic result -- dimensionality predicts
morphology (chains give needles, sheets give plates), cleavage and tabletting
behaviour, and correlates with solubility and humidity stability.

Runs over the ingest cache so it needs no re-parse.

Usage:
    uv run python queries/dimensionality_net.py --limit 5000
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Sequence

# The calculation itself lives in ingest/netdim.py so that build_graph.py can
# stamp net_dim onto every Structure node at ingest time and the UI can query it
# directly. This module is the CLI over it: corpus-wide distribution, plus the
# strong-vs-weak comparison that justifies admitting C-H...A at all.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ingest.netdim import CONSEQUENCE, LABELS, net_dimensionality  # noqa: E402

def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache", type=Path,
                    default=Path("data/jsonl/cod_slice.cache.jsonl"))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--include-inferred", action="store_true",
                    help="include hydrogen bonds with no located hydrogen")
    ap.add_argument("--kinds", default="hbond",
                    help="comma-separated contact kinds to traverse. "
                         "'hbond' is the strong population; add 'hbond_weak' "
                         "to admit C-H...A, which is the comparison that "
                         "decides whether the 0D share is real or an artefact "
                         "of the criteria")
    ap.add_argument("--out", type=Path,
                    default=Path("data/manifest/dimensionality.json"))
    args = ap.parse_args(argv)

    if not args.cache.exists():
        print(f"missing {args.cache}", file=sys.stderr)
        return 1

    kinds = {k.strip() for k in args.kinds.split(",") if k.strip()}

    dist: Counter[int] = Counter()
    examples: dict[int, list[tuple[int, str, int]]] = defaultdict(list)
    rows: list[dict] = []
    n = 0

    with args.cache.open() as fh:
        for line in fh:
            if args.limit and n >= args.limit:
                break
            try:
                res = json.loads(line)
            except json.JSONDecodeError:
                continue
            if res.get("skipped") or not res.get("contacts"):
                continue
            uid_to_site = {a["uid"]: i for i, a in enumerate(res["atoms"])}
            contacts = [
                (c["a"], c["b"], c["symop"])
                for c in res["contacts"]
                if c["kind"] in kinds
                and (args.include_inferred or not c["h_inferred"])
            ]
            if not contacts:
                continue
            dim, biggest, ncomp = net_dimensionality(
                res["spacegroup_number"], contacts, uid_to_site
            )
            n += 1
            dist[dim] += 1
            rows.append({
                "cod_id": res["cod_id"], "dimensionality": dim,
                "hm": res["spacegroup_hm"], "n_hbonds": len(contacts),
            })
            if len(examples[dim]) < 5:
                examples[dim].append(
                    (res["cod_id"], res["spacegroup_hm"], len(contacts))
                )

    print(f"contact kinds traversed: {', '.join(sorted(kinds))}")
    print(f"structures with at least one qualifying hydrogen bond: {n:,}")
    print(f"(inferred-H bonds {'INCLUDED' if args.include_inferred else 'excluded'} "
          f"- a motif claim resting on unrefined hydrogens would not survive review)\n")
    print(f"{'dimensionality':<20}{'structures':>12}{'share':>9}   consequence")
    print("-" * 96)
    for d in (0, 1, 2, 3):
        c = dist.get(d, 0)
        print(f"{LABELS[d]:<20}{c:>12,}{100 * c / max(n, 1):>8.1f}%   {CONSEQUENCE[d]}")

    print("\nexamples:")
    for d in (0, 1, 2, 3):
        for cod, hm, nb in examples.get(d, [])[:3]:
            print(f"  {LABELS[d]:<18} COD {cod:<9} {hm:<14} {nb} H-bonds")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(
        {"counts": {LABELS[k]: v for k, v in sorted(dist.items())},
         "n_structures": n, "include_inferred": args.include_inferred,
         "kinds": sorted(kinds),
         "per_structure": rows}, indent=2) + "\n")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
