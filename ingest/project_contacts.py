"""Project the full graph down to atoms + hydrogen bonds only.

TuringDB rejects an edge-type filter on a variable-length path:

    MATCH (a:Atom)-[e:CONTACT]->{1,8}(b:Atom)
      -> ANALYZE_ERROR: Edge type filters are not supported with
         variable-length paths

Untyped quantifiers work fine. So to traverse *the hydrogen-bond network* we
materialise a graph in which hydrogen bonds are the only edges between atoms;
then an untyped quantifier is exactly a hydrogen-bond traversal, with no
possibility of the walk wandering down a covalent bond.

This is a projection, not a second copy of the data: same atom uids, same edge
properties, derived deterministically from the same JSONL. It is worth being
explicit about on the History screen, because it is a visible modelling
decision a CCDC engineer would reasonably ask about.

By default only hydrogen bonds with a located hydrogen are included
(`h_inferred = false`), since a dimensionality claim resting on inferred
hydrogen positions would not survive scrutiny. Pass --include-inferred to
build the wider network instead.

Usage:
    uv run python -m ingest.project_contacts \
        --in data/jsonl/cod_slice.jsonl --out data/jsonl/cod_contacts.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Sequence


def project(
    src: Path,
    dst: Path,
    kinds: set[str],
    include_inferred: bool,
    drop_isolated: bool = True,
) -> dict[str, int]:
    """Emit a JSONL containing only Atom nodes and the selected contact edges."""
    atoms: dict[str, dict] = {}         # original node id -> node object
    kept_edges: list[dict] = []
    stats: Counter[str] = Counter()

    with src.open() as fh:
        for line in fh:
            obj = json.loads(line)
            if obj.get("type") == "node":
                if "Atom" in obj.get("labels", []):
                    atoms[obj["id"]] = obj
                    stats["atom_nodes"] += 1
            elif obj.get("type") == "relationship":
                if obj.get("label") != "CONTACT":
                    continue
                props = obj.get("properties", {})
                if props.get("kind") not in kinds:
                    stats["skipped_kind"] += 1
                    continue
                if not include_inferred and props.get("h_inferred") is True:
                    stats["skipped_inferred"] += 1
                    continue
                kept_edges.append(obj)

    # keep only atoms that actually take part, so the traversal graph is not
    # mostly isolated vertices
    used: set[str] = set()
    for e in kept_edges:
        used.add(e["start"]["id"])
        used.add(e["end"]["id"])
    keep_ids = used if drop_isolated else set(atoms)

    # LOAD JSONL requires ids starting at 0 with no gaps, so renumber
    remap: dict[str, int] = {}
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("w") as out:
        for old_id, node in atoms.items():
            if old_id not in keep_ids:
                continue
            new_id = len(remap)
            remap[old_id] = new_id
            out.write(json.dumps({
                "type": "node", "id": str(new_id), "labels": ["Atom"],
                "properties": node.get("properties", {}),
            }, separators=(",", ":")) + "\n")
        stats["nodes_written"] = len(remap)

        n = 0
        for e in kept_edges:
            s, t = remap.get(e["start"]["id"]), remap.get(e["end"]["id"])
            if s is None or t is None:
                continue
            out.write(json.dumps({
                "type": "relationship", "id": str(n), "label": "HBOND",
                "start": {"id": str(s)}, "end": {"id": str(t)},
                "properties": e.get("properties", {}),
            }, separators=(",", ":")) + "\n")
            n += 1
        stats["edges_written"] = n

    return dict(stats)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="src", type=Path,
                    default=Path("data/jsonl/cod_slice.jsonl"))
    ap.add_argument("--out", dest="dst", type=Path,
                    default=Path("data/jsonl/cod_contacts.jsonl"))
    ap.add_argument("--kinds", default="hbond",
                    help="comma-separated contact kinds to keep")
    ap.add_argument("--include-inferred", action="store_true",
                    help="also keep hydrogen bonds with no located H")
    ap.add_argument("--keep-isolated", action="store_true")
    args = ap.parse_args(argv)

    if not args.src.exists():
        print(f"missing {args.src}; run ingest.build_graph first", file=sys.stderr)
        return 1

    kinds = {k.strip() for k in args.kinds.split(",") if k.strip()}
    stats = project(args.src, args.dst, kinds, args.include_inferred,
                    drop_isolated=not args.keep_isolated)

    print(f"projection -> {args.dst}")
    print(f"  kinds kept          : {sorted(kinds)}")
    print(f"  inferred H included : {args.include_inferred}")
    for k in ("atom_nodes", "skipped_kind", "skipped_inferred",
              "nodes_written", "edges_written"):
        if k in stats:
            print(f"  {k:<20}: {stats[k]:,}")
    print(f"  size                : {args.dst.stat().st_size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
