"""Hydrogen-bond network dimensionality.

What a crystallographer gets from this
--------------------------------------
Seed on one atom, walk the hydrogen-bond network outward, and count how many
atoms are reachable at each depth. The *shape of the growth curve* classifies
the packing:

    saturates after 2-4 atoms   isolated dimer      (0D)
    roughly linear in depth     chain               (1D)
    roughly quadratic           sheet               (2D)
    roughly cubic               framework           (3D)

That classification is not a curiosity. It predicts crystal morphology (chains
give needles, sheets give plates), cleavage and tabletting behaviour, and it
correlates with solubility and humidity stability. Deciding it today means
recomputing the packing geometrically for every candidate structure and then
running a connectivity analysis over the result. Here it is a traversal.

Why this is the graph argument
------------------------------
The hydrogen bonds themselves are the point. COD and the CSD store atoms and
coordinates; the contact network is *derived* every time anyone asks. We
computed it once at ingest, with the generating symmetry operation on every
edge, so the packing question becomes reachability rather than geometry.

Dialect notes (see docs/DIALECT.md)
-----------------------------------
TuringDB rejects an edge-type filter on a variable-length path
(`Edge type filters are not supported with variable-length paths`), so this runs
against a contact-only projection of the graph in which an untyped quantifier
*is* a hydrogen-bond traversal. The quantifier is TuringDB's postfix form
`-[e]->{k,k}`, not Neo4j's `-[e*k..k]->`.

There is also no `length(path)` and no grouping aggregate, so the growth curve is
built by issuing one exact-depth query per depth and collecting the counts
client-side. Each query reports its own server-side execution time.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field
from typing import Sequence

from turingdb import TuringDB

DEFAULT_HOST = "http://localhost:6691"
DEFAULT_GRAPH = "cod_contacts"
MAX_DEPTH = 8


@dataclass
class Curve:
    """Reachability growth from one seed atom."""

    seed_uid: str
    cod_id: int | None
    reached: list[int] = field(default_factory=list)      # cumulative, per depth
    ms: list[float] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.reached[-1] if self.reached else 0

    def classify(self) -> tuple[str, str]:
        """Classify the packing motif from the growth curve.

        Deliberately conservative and explained rather than fitted: we compare
        successive increments, because that is what distinguishes a chain (an
        increment that stays roughly constant) from a sheet (one that grows
        roughly linearly) from a framework (one that keeps accelerating).
        """
        r = self.reached
        if not r or r[-1] < 2:
            return "isolated", "no hydrogen-bonded neighbours found"
        # per-depth increments
        inc = [r[0]] + [r[i] - r[i - 1] for i in range(1, len(r))]
        tail = [x for x in inc[2:] if x > 0]
        if r[-1] <= 4 or not tail:
            return "0D dimer / finite cluster", (
                f"growth stops at {r[-1]} atoms - an isolated motif, "
                "typically more soluble"
            )
        # is the increment still growing late in the walk?
        early = statistics.mean(inc[2:4]) if len(inc) >= 4 else inc[-1]
        late = statistics.mean(inc[-2:])
        ratio = late / early if early else 1.0
        if ratio < 1.35:
            return "1D chain", (
                "reachability grows by a roughly constant amount per hop, "
                "which is a chain - often needle morphology"
            )
        if ratio < 2.5:
            return "2D sheet", (
                "the per-hop increment itself grows roughly linearly, which is "
                "a layer - plate morphology, easy cleavage"
            )
        return "3D framework", (
            "the increment keeps accelerating, which is a three-dimensional "
            "network - typically harder and less soluble"
        )


CYPHER_AT_DEPTH = (
    # Untyped quantifier: in cod_contacts the only edges are hydrogen bonds,
    # so this IS a hydrogen-bond traversal. TuringDB does not accept
    # -[e:CONTACT]->{{k,k}}.
    "MATCH (a:Atom {{uid:'{uid}'}})-[e]->{{{k},{k}}}(b:Atom) RETURN count(b)"
)


def growth_curve(
    client: TuringDB, seed_uid: str, max_depth: int = MAX_DEPTH
) -> Curve:
    """Cumulative reachable-atom count at each depth from one seed."""
    curve = Curve(seed_uid=seed_uid, cod_id=None)
    seen_total = 0
    for k in range(1, max_depth + 1):
        q = CYPHER_AT_DEPTH.format(uid=seed_uid, k=k)
        try:
            n = int(client.query(q).iloc[0, 0])
            ms = client.get_query_exec_time() or 0.0
        except Exception:  # noqa: BLE001
            break
        # exact-depth counts are walks, not distinct atoms; accumulate the
        # monotone envelope so the curve is interpretable as "reached by depth k"
        seen_total = max(seen_total, n)
        curve.reached.append(seen_total)
        curve.ms.append(round(ms, 3))
    return curve


def pick_seeds(client: TuringDB, n: int) -> list[tuple[str, int]]:
    """Atoms that actually participate in a hydrogen bond."""
    df = client.query(
        "MATCH (a:Atom)-[e]->(b:Atom) RETURN a.uid, a.cod_id LIMIT " + str(n * 40)
    )
    out: list[tuple[str, int]] = []
    seen: set[str] = set()
    per_structure: set[int] = set()
    for uid, cod in zip(df.iloc[:, 0], df.iloc[:, 1]):
        uid = str(uid)
        try:
            cod_i = int(cod)
        except Exception:  # noqa: BLE001
            continue
        # one seed per structure keeps the sample from being 40 atoms of the
        # same crystal
        if uid in seen or cod_i in per_structure:
            continue
        seen.add(uid)
        per_structure.add(cod_i)
        out.append((uid, cod_i))
        if len(out) >= n:
            break
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--graph", default=DEFAULT_GRAPH)
    ap.add_argument("--seeds", type=int, default=12)
    ap.add_argument("--depth", type=int, default=MAX_DEPTH)
    ap.add_argument("--uid", default=None, help="run a single named seed atom")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    client = TuringDB(host=args.host)
    try:
        client.query(f"LOAD GRAPH {args.graph}")
    except Exception:  # noqa: BLE001
        pass
    client.set_graph(args.graph)

    n_atoms = int(client.query("MATCH (n:Atom) RETURN count(n)").iloc[0, 0])
    n_edges = int(client.query("MATCH ()-[r]->() RETURN count(r)").iloc[0, 0])
    print(f"graph {args.graph}: {n_atoms:,} atoms, {n_edges:,} hydrogen bonds\n")

    seeds = ([(args.uid, None)] if args.uid
             else pick_seeds(client, args.seeds))
    if not seeds:
        print("no hydrogen-bonded atoms found")
        return 1

    curves: list[Curve] = []
    header = "  " + "".join(f"{d:>8}" for d in range(1, args.depth + 1))
    print(f"{'seed atom':<26}{'COD':>9} {header}   motif")
    print("-" * (26 + 9 + 8 * args.depth + 24))
    for uid, cod in seeds:
        c = growth_curve(client, uid, args.depth)
        c.cod_id = cod
        curves.append(c)
        motif, _why = c.classify()
        cells = "".join(f"{v:>8,}" for v in c.reached)
        pad = " " * (8 * (args.depth - len(c.reached)))
        print(f"{uid:<26}{str(cod or '-'):>9}  {cells}{pad}   {motif}")

    all_ms = [m for c in curves for m in c.ms]
    if all_ms:
        all_ms.sort()
        print(f"\nserver-side latency over {len(all_ms)} traversals: "
              f"p50 {all_ms[len(all_ms) // 2]:.2f} ms, "
              f"p95 {all_ms[int(len(all_ms) * 0.95)]:.2f} ms, "
              f"max {all_ms[-1]:.2f} ms")

    print("\ninterpretation:")
    for c in curves[:6]:
        motif, why = c.classify()
        print(f"  {c.seed_uid:<26} {motif:<26} {why}")

    if args.json:
        print(json.dumps([{
            "seed": c.seed_uid, "cod_id": c.cod_id, "reached": c.reached,
            "ms": c.ms, "motif": c.classify()[0],
        } for c in curves], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
