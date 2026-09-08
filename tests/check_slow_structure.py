"""Regression check for the structures that used to stall the ingest.

COD 4504526 (147 sites, P2(1)/c) has an 85-atom organic component whose
bond-order perception never returned with RDKit's default unlimited iteration
budget. Kept as a named test so the fix does not silently regress.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from ingest.build_graph import process_structure
from ingest.download import cod_cif_relpath

#: (cod_id, generous wall-clock budget in seconds)
CASES: list[tuple[int, float]] = [
    (4504526, 30.0),
]


def main() -> int:
    ok = True
    for cod_id, budget in CASES:
        path = Path("data/cif") / cod_cif_relpath(cod_id)
        if not path.exists():
            print(f"  SKIP  COD {cod_id} (CIF not on disk)")
            continue
        t0 = time.perf_counter()
        res = process_structure((str(path), cod_id))
        dt = time.perf_counter() - t0
        if res is None:
            print(f"  FAIL  COD {cod_id}: returned None")
            ok = False
            continue
        skipped = res.get("skipped")
        n_comp = len(res.get("components", []))
        n_con = len(res.get("contacts", []))
        n_ik = sum(1 for c in res.get("components", []) if c.get("inchikey"))
        verdict = "PASS" if dt < budget else "FAIL"
        if dt >= budget:
            ok = False
        print(f"  {verdict}  COD {cod_id}: {dt:.2f}s (budget {budget:.0f}s) "
              f"components={n_comp} with_inchikey={n_ik} contacts={n_con} "
              f"skipped={skipped}")
        if res.get("stats", {}).get("timed_out"):
            print("        note: contact search hit its time budget "
                  "(contacts_truncated will be set)")
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
