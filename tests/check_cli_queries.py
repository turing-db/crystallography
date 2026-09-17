"""Execute every named query the CLI ships, against a live server.

Same purpose as tests/check_frontend_queries.py, for the other surface. A
query that no longer parses -- because a property was renamed, or a graph was
rebuilt without it -- should fail here rather than in front of someone.

    python tests/check_cli_queries.py
    python tests/check_cli_queries.py --host http://localhost:6691
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import crystal  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=crystal.DEFAULT_HOST)
    ap.add_argument("--graph", default=crystal.DEFAULT_GRAPH)
    a = ap.parse_args(argv)

    failed: list[tuple[str, str]] = []
    print(f"{'query':<18}{'rows':>10}{'ms':>9}")
    print("-" * 37)
    for name, (_desc, cypher, _tally) in crystal.LIBRARY.items():
        t0 = time.perf_counter()
        try:
            _, rows = crystal.query(cypher, graph=a.graph, host=a.host)
        except Exception as exc:  # noqa: BLE001
            failed.append((name, str(exc)[:160]))
            print(f"{name:<18}{'ERROR':>10}")
            continue
        print(f"{name:<18}{len(rows):>10,}{(time.perf_counter() - t0) * 1000:>9.0f}")

    if failed:
        print(f"\n{len(failed)} FAILING:")
        for name, err in failed:
            print(f"  {name}: {err}")
        return 1
    print(f"\nall {len(crystal.LIBRARY)} named queries execute cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
