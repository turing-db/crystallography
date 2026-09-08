"""Statistical validation of symop recovery across many real COD entries.

Every contact edge in the graph carries a `symop`. If that operation does not
regenerate the neighbouring atom's observed position, the edge is a lie and the
whole periodic-network story collapses. This script re-derives the operation for
a large sample of real neighbour pairs and measures the residual.

Pass criterion: every resolved symop reproduces the neighbour position to well
under 0.01 A, and the unresolved fraction is ~0.
"""

from __future__ import annotations

import glob
import random
import sys
from collections import Counter

import gemmi

from ingest import chemistry as ch
from ingest.symmetry import resolve_symop, spacegroup_of, verify_symop


def main() -> int:
    n_structures = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    paths = sorted(glob.glob("data/cif/**/*.cif", recursive=True))
    if not paths:
        print("no CIFs; run ingest.download first")
        return 1
    sample = random.Random(20260903).sample(paths, min(n_structures, len(paths)))

    n_ok = n_pairs = n_unresolved = 0
    n_parse_fail = n_no_sg = n_done = 0
    worst = 0.0
    worst_where = ""
    residuals: list[float] = []
    op_hist: Counter[str] = Counter()

    for path in sample:
        try:
            st = gemmi.read_small_structure(path)
        except Exception:  # noqa: BLE001
            n_parse_fail += 1
            continue
        sg = spacegroup_of(st)
        if sg is None or not st.sites:
            n_no_sg += 1
            continue
        try:
            st.setup_cell_images()
            ns = gemmi.NeighborSearch(st, ch.MAX_CONTACT_SEARCH_RADIUS).populate()
        except Exception:  # noqa: BLE001
            n_parse_fail += 1
            continue
        n_done += 1

        for site in st.sites[:12]:
            try:
                marks = ns.find_site_neighbors(site, min_dist=0.4, max_dist=4.0)
            except Exception:  # noqa: BLE001
                continue
            for mark in list(marks)[:12]:
                try:
                    ref = mark.to_site(st)
                except Exception:  # noqa: BLE001
                    continue
                observed = st.cell.fractionalize(mark.pos)
                n_pairs += 1
                symop = resolve_symop(sg, st.cell, ref.fract, observed)
                if symop is None:
                    n_unresolved += 1
                    continue
                resid = verify_symop(sg, st.cell, ref.fract, observed, symop)
                residuals.append(resid)
                op_hist[symop.cif_code()] += 1
                if resid > worst:
                    worst, worst_where = resid, f"{st.name} {site.label}->{ref.label} {symop.describe()}"
                if resid < 0.01:
                    n_ok += 1

    residuals.sort()
    print(f"structures sampled : {len(sample)}")
    print(f"  usable           : {n_done}")
    print(f"  parse/search fail: {n_parse_fail}")
    print(f"  no spacegroup    : {n_no_sg}")
    print(f"neighbour pairs    : {n_pairs:,}")
    print(f"  symop resolved   : {n_pairs - n_unresolved:,} "
          f"({100 * (n_pairs - n_unresolved) / max(1, n_pairs):.2f}%)")
    print(f"  unresolved       : {n_unresolved:,}")
    print(f"  residual < 0.01 A: {n_ok:,} "
          f"({100 * n_ok / max(1, n_pairs - n_unresolved):.2f}% of resolved)")
    if residuals:
        print(f"  residual p50     : {residuals[len(residuals) // 2]:.2e} A")
        print(f"  residual p99     : {residuals[int(len(residuals) * 0.99)]:.2e} A")
        print(f"  residual max     : {residuals[-1]:.2e} A   ({worst_where})")
    print(f"most common symops : {op_hist.most_common(8)}")

    ok = n_pairs > 0 and n_unresolved == 0 and n_ok == len(residuals)
    print("\nRESULT:", "PASS" if ok else "FAIL - investigate before ingesting")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
