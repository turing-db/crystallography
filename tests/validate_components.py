"""Eyeball component perception on real COD entries.

Prints, per structure: space group, site count, bond count, and the discrete
components with their formulae. A chemist reading this output should recognise
the molecules -- that is the whole point of the check.
"""

from __future__ import annotations

import glob
import random
import sys
from collections import Counter

import gemmi

from ingest import chemistry as ch
from ingest.structure import (
    build_atoms,
    component_instances,
    detect_bonds,
    find_components,
    hill_formula,
    read_structure,
)


def check_op_api() -> None:
    print("=== gemmi.Op API assumptions ===")
    op = gemmi.Op("-x,y+1/2,-z+1/2")
    print(f"  Op.DEN            = {gemmi.Op.DEN}")
    print(f"  op.triplet()      = {op.triplet()}")
    print(f"  op.tran           = {op.tran}")
    print(f"  op.inverse()      = {op.inverse().triplet()}")
    print(f"  op * op           = {(op * op).triplet()}")
    c = gemmi.Op(op.triplet())
    c.tran = (c.tran[0] + gemmi.Op.DEN, c.tran[1], c.tran[2])
    print(f"  tran assignment   = {c.triplet()}")
    print()


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    check_op_api()

    paths = sorted(glob.glob("data/cif/**/*.cif", recursive=True))
    sample = random.Random(7).sample(paths, min(n * 6, len(paths)))

    shown = 0
    stats: Counter[str] = Counter()
    for path in sample:
        if shown >= n:
            break
        got = read_structure(path)
        if got is None:
            stats["unreadable"] += 1
            continue
        st, sg = got
        cod_id = int(st.name) if str(st.name).isdigit() else 0
        atoms = build_atoms(st, cod_id)
        if not (6 <= len(atoms) <= 90):
            continue
        bonds, bnotes = detect_bonds(st, sg, atoms)
        comps, polymeric, cnotes = find_components(st, sg, atoms, bonds)

        print(f"--- {cod_id}  {st.spacegroup_hm} (#{sg.number}, {sg.crystal_system_str()})"
              f"  sites={len(atoms)} bonds={len(bonds)} components={len(comps)}")
        for ci, sites in enumerate(comps):
            if ci in polymeric:
                counts = Counter(atoms[i].element for i in sites)
                print(f"      [{ci}] POLYMERIC/extended  asym formula {hill_formula(counts)}"
                      f"  ({len(sites)} asym sites)")
                stats["polymeric"] += 1
                continue
            inst = component_instances(sg, atoms, bonds, sites)
            counts = Counter(atoms[i].element for i, _ in inst)
            heavy = sum(v for k, v in counts.items() if k != "H")
            print(f"      [{ci}] {hill_formula(counts):<28} atoms={len(inst):<4} heavy={heavy}"
                  f"  asym_sites={len(sites)}")
            stats["finite"] += 1
        for note in bnotes + cnotes:
            print(f"      note: {note}")
        shown += 1

    print(f"\ncomponents: {stats['finite']} finite, {stats['polymeric']} polymeric; "
          f"{stats['unreadable']} unreadable structures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
