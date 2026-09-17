"""Chemical sanity of the perceived bond graph, across many real structures.

Bond detection is purely geometric, so the honest way to test it is to ask
whether the resulting valences are chemically possible. Hydrogen must have
exactly one bond; carbon almost always four; oxygen one or two; nitrogen three
or four. Systematic deviation means the radii or the tolerance are wrong.

This is the statistical companion to the hand-checked structures in
tests/test_known_structures.py.
"""

from __future__ import annotations

import glob
import random
import sys
from collections import Counter, defaultdict

import gemmi

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingest import chemistry as ch
from ingest.structure import build_atoms, detect_bonds, find_components, read_structure


def _is_metal(symbol: str) -> bool:
    try:
        return bool(gemmi.Element(symbol).is_metal)
    except Exception:  # noqa: BLE001
        return False

#: Valences a chemist would accept for a PURELY ORGANIC environment, i.e. an
#: atom with no metal among its neighbours. Carbon is commonly 3 rather than 4
#: because an aromatic C-H carbon has two ring neighbours plus one hydrogen, and
#: because many COD entries carry no hydrogen positions at all.
#:
#: Atoms that DO have a metal neighbour are reported separately: a carboxylate
#: oxygen bridging two nickels legitimately shows three connections, and a
#: perchlorate chlorine legitimately shows four. Judging those against organic
#: valences would be measuring the wrong thing, and widening the table until
#: everything passed would hide real over-bonding.
EXPECTED: dict[str, set[int]] = {
    "H": {1},
    "C": {3, 4},
    "N": {1, 2, 3, 4},
    # 0 is included deliberately: it is overwhelmingly solvent water whose
    # hydrogens were never located. Verified by inspection -- the offenders carry
    # labels like O5W (the "W" water convention) and partial occupancies such as
    # 0.636, and ZERO of them occur in structures that have no H sites at all,
    # i.e. it is not "this structure omits hydrogen" but "this water's H was not
    # refined". A heavy-atom bond graph cannot connect such an oxygen.
    "O": {0, 1, 2},
    "F": {1},
    "Cl": {0, 1, 3, 4},   # 0 chloride, 1 organochlorine, 3 chlorate, 4 perchlorate
    "Br": {0, 1},
    "I": {0, 1, 2},       # 2 = triiodide
    "S": {1, 2, 3, 4, 6},
}


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    paths = sorted(glob.glob("data/cif/**/*.cif", recursive=True))
    if not paths:
        print("no CIFs under data/cif -- run `python -m ingest.download "
              "--dataset slice` first", file=sys.stderr)
        return 1
    sample = random.Random(11).sample(paths, min(n, len(paths)))

    valence: dict[str, Counter[int]] = defaultdict(Counter)
    valence_metal: dict[str, Counter[int]] = defaultdict(Counter)
    n_struct = n_skip = 0
    isolated_heavy = 0
    total_heavy = 0
    comp_sizes: Counter[int] = Counter()
    n_polymeric = 0

    for path in sample:
        got = read_structure(path)
        if got is None:
            n_skip += 1
            continue
        st, sg = got
        cod_id = int(st.name) if str(st.name).isdigit() else 0
        atoms = build_atoms(st, cod_id)
        if len(atoms) > 300:
            continue
        try:
            bonds, _ = detect_bonds(st, sg, atoms)
            comps, poly, _ = find_components(st, sg, atoms, bonds)
        except Exception:  # noqa: BLE001
            n_skip += 1
            continue
        n_struct += 1
        n_polymeric += len(poly)

        deg: Counter[int] = Counter()
        has_metal_neighbour: set[int] = set()
        for b in bonds:
            deg[b.a_index] += 1
            deg[b.b_index] += 1
            if _is_metal(atoms[b.b_index].element):
                has_metal_neighbour.add(b.a_index)
            if _is_metal(atoms[b.a_index].element):
                has_metal_neighbour.add(b.b_index)
        for a in atoms:
            if a.occupancy < ch.MIN_BONDING_OCCUPANCY:
                continue
            d = deg.get(a.site_index, 0)
            if a.element in EXPECTED:
                if a.site_index in has_metal_neighbour:
                    valence_metal[a.element][d] += 1
                else:
                    valence[a.element][d] += 1
            if a.element != "H":
                total_heavy += 1
                if d == 0 and a.element not in ("Cl", "Br", "I", "F"):
                    isolated_heavy += 1
        for ci, sites in enumerate(comps):
            if ci not in poly:
                comp_sizes[len(sites)] += 1

    print(f"structures analysed : {n_struct}  (skipped {n_skip})")
    print(f"polymeric components: {n_polymeric}")
    print(f"isolated heavy atoms: {isolated_heavy} / {total_heavy} "
          f"({100 * isolated_heavy / max(1, total_heavy):.2f}%)  "
          f"[excludes halide counter-ions]")
    print("\nvalence in PURELY ORGANIC environments (no metal neighbour):")
    # A validator that analysed nothing must not report PASS. Without this,
    # every element is skipped on an empty corpus, `ok` is never cleared, and
    # the script exits 0 having checked zero structures -- which reads exactly
    # like a clean run.
    if not any(valence.get(el) for el in EXPECTED):
        print("  no atoms analysed -- run `python -m ingest.download --dataset slice` "
              "first", file=sys.stderr)
        return 1
    ok = True
    for el in ("H", "C", "N", "O", "F", "S", "Cl", "Br", "I"):
        counts = valence.get(el)
        if not counts:
            continue
        total = sum(counts.values())
        good = sum(v for k, v in counts.items() if k in EXPECTED[el])
        pct = 100 * good / total
        top = ", ".join(f"{k}:{100 * v / total:.1f}%" for k, v in
                        sorted(counts.items(), key=lambda kv: -kv[1])[:5])
        flag = "" if pct >= 95 else "   <-- LOW"
        if pct < 95:
            ok = False
        print(f"  {el:<3} n={total:<7} plausible={pct:5.1f}%   {top}{flag}")

    print("\nvalence for METAL-COORDINATED atoms (judged separately, not scored):")
    for el in ("O", "N", "S", "Cl", "Br", "I", "F"):
        counts = valence_metal.get(el)
        if not counts:
            continue
        total = sum(counts.values())
        top = ", ".join(f"{k}:{100 * v / total:.1f}%" for k, v in
                        sorted(counts.items(), key=lambda kv: -kv[1])[:5])
        print(f"  {el:<3} n={total:<7} {top}")

    common = ", ".join(f"{k} sites:{v}" for k, v in comp_sizes.most_common(6))
    print(f"\nmost common component sizes: {common}")
    print("\nRESULT:", "PASS" if ok else "REVIEW - valences look off")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
