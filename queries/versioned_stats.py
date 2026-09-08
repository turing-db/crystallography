"""The same statistic, recomputed against the corpus as it stood in each year.

What a crystallographer gets from this
--------------------------------------
A published crystal-engineering result of the form "N% of structures containing
X form motif Y" is only reproducible against the database as it stood when it
was computed. Today that means archiving database dumps and re-running scripts
against them. Here it is a checkout: `cod@2010` *is* the literature to the end
of 2010, so the number recomputes exactly.

Two things fall out that are interesting in their own right:

* **Motif frequency drifts.** Which functional groups the literature reports is
  not stationary -- it tracks fashion in crystal engineering and changes in what
  journals accept.
* **Refinement practice improves.** The share of structures with refined
  hydrogen positions is a proxy for data quality, and it moves over 25 years.
  That matters here because our own hydrogen-bond criterion falls back to a
  heavy-atom cutoff whenever hydrogens are missing, so the *measurable* fraction
  of the contact network is itself a function of publication year.

The audit variant
-----------------
Given a structure, report which snapshots contained it. That is the question a
compliance-minded reviewer asks about a corrected or retracted entry: which
published analyses are now suspect.

Dialect notes
-------------
No grouping aggregate exists (`RETURN f.fragment_type, count(c)` is rejected),
so each frequency is its own `count()` query and the table is assembled here.
Time travel is `checkout(commit=<hash>)`, which issues `LOAD COMMIT` and then
pins the commit for subsequent reads.

Usage:
    uv run python queries/versioned_stats.py
    uv run python queries/versioned_stats.py --audit 2229029
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from turingdb import TuringDB

DEFAULT_HOST = "http://localhost:6691"
DATA_GRAPH = "cod_versioned"
LEDGER_GRAPH = "cod_versions"

#: The motifs worth tracking. Deliberately the hydrogen-bonding ones plus the
#: halogen, since those are what the contact criteria act on.
#: Below this many components a cohort percentage is not worth quoting.
MIN_COHORT = 400

MOTIFS = [
    "carboxylic_acid", "carboxylate", "amide_secondary", "amide_primary",
    "hydroxyl", "pyridine_nitrogen", "amine_primary", "nitro_charged",
    "sulfonamide", "halide", "carbonyl", "aromatic_ring", "ether", "ester",
]


def scalar(client: TuringDB, cypher: str) -> float:
    df = client.query(cypher)
    if not len(df):
        return 0.0
    v = df.iloc[0, 0]
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def load_ledger(client: TuringDB) -> list[dict]:
    try:
        client.query(f"LOAD GRAPH {LEDGER_GRAPH}")
    except Exception:  # noqa: BLE001
        pass
    client.set_graph(LEDGER_GRAPH)
    df = client.query(
        "MATCH (s:Snapshot) RETURN s.year, s.tag, s.commit_hash, "
        "s.structures_added, s.structures_total ORDER BY s.year"
    )
    out = []
    for i in range(len(df)):
        out.append({
            "year": int(df.iloc[i, 0]),
            "tag": str(df.iloc[i, 1]),
            "commit": str(df.iloc[i, 2]),
            "added": int(df.iloc[i, 3]),
            "total": int(df.iloc[i, 4]),
        })
    return out


def snapshot_stats(client: TuringDB, commit: str) -> dict:
    """Recompute the whole statistic set against one commit."""
    client.checkout(commit=commit)
    total = scalar(client, "MATCH (s:Structure) RETURN count(s)")
    with_h = scalar(
        client, "MATCH (s:Structure) WHERE s.has_hydrogens = true RETURN count(s)"
    )
    with_hb = scalar(
        client, "MATCH (s:Structure) WHERE s.n_hbonds > 0 RETURN count(s)"
    )
    located = scalar(
        client, "MATCH (s:Structure) WHERE s.n_hbonds_located > 0 RETURN count(s)"
    )
    multi = scalar(
        client, "MATCH (s:Structure) WHERE s.n_components > 1 RETURN count(s)"
    )
    good_r = scalar(
        client, "MATCH (s:Structure) WHERE s.r_factor < 0.05 RETURN count(s)"
    )
    has_r = scalar(
        client, "MATCH (s:Structure) WHERE s.has_r_factor = true RETURN count(s)"
    )
    motifs: dict[str, float] = {}
    for m in MOTIFS:
        motifs[m] = scalar(
            client,
            "MATCH (c:Component)-[:HAS_FRAGMENT]->(f:Fragment) "
            f"WHERE f.fragment_type = '{m}' RETURN count(c)"
        )
    comps = scalar(client, "MATCH (c:Component) RETURN count(c)")
    return {
        "structures": total, "with_hydrogens": with_h, "with_hbond": with_hb,
        "with_located_hbond": located, "multi_component": multi,
        "r_under_005": good_r, "has_r_factor": has_r, "components": comps,
        "motifs": motifs,
    }


def pct(a: float, b: float) -> str:
    return f"{100 * a / b:.1f}" if b else "—"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--tags", default="2003,2008,2013,2018,2023,2026",
                    help="comma-separated years to compare")
    ap.add_argument("--audit", type=int, default=None,
                    help="report which snapshots contain this COD id")
    ap.add_argument("--out", type=Path,
                    default=Path("data/manifest/versioned_stats.json"))
    args = ap.parse_args(argv)

    client = TuringDB(host=args.host)
    ledger = load_ledger(client)
    if not ledger:
        print("no snapshots found; run ingest.build_versioned first", file=sys.stderr)
        return 1
    by_year = {r["year"]: r for r in ledger}

    # ------------------------------------------------------------------ audit
    if args.audit is not None:
        cod = args.audit
        try:
            client.query(f"LOAD GRAPH {DATA_GRAPH}")
        except Exception:  # noqa: BLE001
            pass
        client.set_graph(DATA_GRAPH)
        print(f"audit trail for COD {cod}\n")
        print(f"  {'tag':<11}{'commit':<20}{'present':>9}{'corpus':>9}")
        first = None
        for row in ledger:
            client.checkout(commit=row["commit"])
            n = scalar(client, f"MATCH (s:Structure) WHERE s.cod_id = {cod} RETURN count(s)")
            tot = scalar(client, "MATCH (s:Structure) RETURN count(s)")
            here = n > 0
            if here and first is None:
                first = row["tag"]
            print(f"  {row['tag']:<11}{row['commit'][:18]:<20}"
                  f"{'yes' if here else 'no':>9}{int(tot):>9,}")
        client.checkout()
        if first:
            print(f"\n  first appears at {first}. Every snapshot from that tag "
                  f"onward contains it, so any statistic computed against those "
                  f"commits includes this determination -- which is exactly the "
                  f"set of analyses to re-examine if it is later corrected or "
                  f"retracted.")
        else:
            print(f"\n  COD {cod} is not in this versioned graph.")
        return 0

    # ------------------------------------------------------- statistics table
    years = []
    for t in args.tags.split(","):
        t = t.strip()
        if t.isdigit() and int(t) in by_year:
            years.append(int(t))
    if not years:
        years = [ledger[0]["year"], ledger[-1]["year"]]

    # LOAD COMMIT resolves against the CURRENT graph, and load_ledger left us on
    # the ledger graph -- whose history knows nothing about the data graph's
    # commits ("Commit hash not found").
    try:
        client.query(f"LOAD GRAPH {DATA_GRAPH}")
    except Exception:  # noqa: BLE001
        pass
    client.set_graph(DATA_GRAPH)

    print(f"recomputing the same statistics against {len(years)} commits\n")
    stats: dict[int, dict] = {}
    for y in years:
        row = by_year[y]
        stats[y] = snapshot_stats(client, row["commit"])
        print(f"  {row['tag']:<10} {row['commit'][:16]}  "
              f"{int(stats[y]['structures']):>6,} structures")
    client.checkout()

    hdr = "".join(f"{y:>12}" for y in years)
    print(f"\n{'corpus':<30}{hdr}")
    print("-" * (30 + 12 * len(years)))
    for label, key in (
        ("structures", "structures"),
        ("distinct components", "components"),
        ("multi-component (salt/solvate)", "multi_component"),
    ):
        print(f"{label:<30}" + "".join(
            f"{int(stats[y][key]):>12,}" for y in years))

    print(f"\n{'refinement practice (% of corpus)':<30}{hdr}")
    print("-" * (30 + 12 * len(years)))
    for label, key in (
        ("H positions refined", "with_hydrogens"),
        ("R factor < 0.05", "r_under_005"),
        ("any hydrogen bond found", "with_hbond"),
        ("H-bond with located H", "with_located_hbond"),
    ):
        print(f"{label:<30}" + "".join(
            f"{pct(stats[y][key], stats[y]['structures']):>12}" for y in years))

    print(f"\n{'motif frequency (% of components)':<30}{hdr}")
    print("-" * (30 + 12 * len(years)))
    last = years[-1]
    ranked = sorted(MOTIFS, key=lambda m: -stats[last]["motifs"][m])
    drift: list[tuple[str, float]] = []
    for m in ranked:
        cells = "".join(
            f"{pct(stats[y]['motifs'][m], stats[y]['components']):>12}" for y in years
        )
        print(f"{m.replace('_', ' '):<30}{cells}")
        a = 100 * stats[years[0]]["motifs"][m] / max(stats[years[0]]["components"], 1)
        b = 100 * stats[last]["motifs"][m] / max(stats[last]["components"], 1)
        drift.append((m, b - a))

    drift.sort(key=lambda kv: -abs(kv[1]))
    print(f"\nlargest drift between cod@{years[0]} and cod@{last} "
          f"(percentage points of the component population):")
    for m, d in drift[:6]:
        arrow = "up  " if d > 0 else "down"
        print(f"  {m.replace('_', ' '):<24} {arrow} {abs(d):5.2f} pp")

    # ---------------------------------------------------------------- cohorts
    # The table above is CUMULATIVE: each commit is the corpus up to that year,
    # so consecutive snapshots share almost all their content and year-on-year
    # change is damped by construction. Most of the apparent 2003 -> 2008 "drift"
    # is just the 2003 sample being small (a few hundred components).
    #
    # To see whether the literature actually changed, ask each year's COHORT --
    # only the structures published that year. That needs no time travel; it is
    # a filter at HEAD. Showing both is the honest way to present this.
    client.checkout()
    print(f"\n{'motif frequency WITHIN each year cohort (%)':<30}{hdr}")
    print("-" * (30 + 12 * len(years)))
    cohort: dict[int, dict[str, float]] = {}
    cohort_n: dict[int, float] = {}
    for y in years:
        cohort_n[y] = scalar(
            client,
            "MATCH (s:Structure)-[:CONTAINS_COMPONENT]->(c:Component) "
            f"WHERE s.year = {y} RETURN count(c)"
        )
        cohort[y] = {}
        for m in MOTIFS:
            cohort[y][m] = scalar(
                client,
                "MATCH (s:Structure)-[:CONTAINS_COMPONENT]->(c:Component), "
                "(c)-[:HAS_FRAGMENT]->(f:Fragment) "
                f"WHERE s.year = {y} AND f.fragment_type = '{m}' RETURN count(c)"
            )
    print(f"{'components in cohort':<30}" + "".join(
        f"{int(cohort_n[y]):>12,}" for y in years))
    thin = [y for y in years if cohort_n[y] < MIN_COHORT]
    print(f"{'  (thin cohort, see note)':<30}" + "".join(
        f"{('  <-- thin' if y in thin else ''):>12}" for y in years))
    cdrift: list[tuple[str, float]] = []
    for m in ranked:
        print(f"{m.replace('_', ' '):<30}" + "".join(
            f"{pct(cohort[y][m], cohort_n[y]):>12}" for y in years))
        a = 100 * cohort[years[0]][m] / max(cohort_n[years[0]], 1)
        b = 100 * cohort[last][m] / max(cohort_n[last], 1)
        cdrift.append((m, b - a))
    cdrift.sort(key=lambda kv: -abs(kv[1]))
    endpoints_ok = cohort_n[years[0]] >= MIN_COHORT and cohort_n[last] >= MIN_COHORT
    print(f"\nlargest COHORT drift, {years[0]} vs {last} (percentage points):")
    for m, d in cdrift[:6]:
        print(f"  {m.replace('_', ' '):<24} {'up  ' if d > 0 else 'down'} {abs(d):5.2f} pp")
    if not endpoints_ok:
        print(f"\n  DO NOT QUOTE THESE. One or both endpoint cohorts hold fewer\n"
              f"  than {MIN_COHORT} components ({int(cohort_n[years[0]])} in "
              f"{years[0]}, {int(cohort_n[last])} in {last}), and a percentage\n"
              f"  over a few dozen molecules moves several points on one entry.\n"
              f"  The cohort series is genuinely noisy at both tails: the corpus\n"
              f"  starts small and Acta E's volume collapsed after 2015, and 2026\n"
              f"  is a partial year. Compare well-populated cohorts instead, e.g.\n"
              f"  --tags 2007,2009,2011,2013 (600-900 structures each).\n\n"
              f"  What IS demonstrated here is the mechanism: one query, six\n"
              f"  points in the graph's own history, no archived dumps. The\n"
              f"  cumulative table above is the reproducibility claim; the cohort\n"
              f"  table is a research question this corpus is too thin to settle.")

    print("\nRead this as: the same query, against the same graph, at six points "
          "in its own history.\nNo dumps were archived and no scripts were "
          "re-run against them -- the corpus itself\nrewinds.")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "graph": DATA_GRAPH, "tags": years,
        "commits": {y: by_year[y]["commit"] for y in years},
        "stats": {str(y): stats[y] for y in years},
        "cohort_components": {str(y): cohort_n[y] for y in years},
        "cohort_motifs": {str(y): cohort[y] for y in years},
    }, indent=2) + "\n")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
