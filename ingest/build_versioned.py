"""Build the corpus chronologically: one commit per publication year.

Why this exists
---------------
Crystal structures get re-refined, corrected and retracted, and published
statistics computed over a structural database are only reproducible against the
database *as it stood at the time*. The usual answer is to archive full database
dumps. Here the graph itself is rewindable: check out `cod@2010` and the corpus
is exactly the literature to the end of 2010, so a statistic recomputed then
still reproduces.

Two things TuringDB does not have, and how they are handled
-----------------------------------------------------------
* **No tags.** `CREATE TAG` / `LIST TAG` are parse errors and the SDK has no tag
  method; the only primitives are integer change ids, commit hashes and a single
  `main`. So the year -> commit mapping is kept in a small second graph
  (`cod_versions`), which makes the ledger itself queryable and versioned rather
  than a JSON file next to the data.
* **No `LOAD JSONL` for increments.** `LOAD JSONL` builds a whole graph in one
  statement, so it cannot express "add this year on top of what is there". The
  chronological build therefore goes through the change workflow with `CREATE`.

Shape of each year's commit
---------------------------
    new_change() -> CREATE nodes -> COMMIT -> per-structure edge CREATEs
                 -> CHANGE SUBMIT -> read HEAD from CALL db.history()

The intermediate `COMMIT` is required: a `MATCH` inside a change cannot see
nodes `CREATE`d earlier in that same change, so the edge statements would
silently match nothing without it.

This graph is deliberately lean -- no atoms, no contact edges. It answers
questions *about the corpus over time* (motif frequency, space-group
distribution, refinement quality, hydrogen-position coverage), and per-structure
contact totals are carried as properties so those trends stay answerable without
half a million atom nodes in every commit.

Usage:
    uv run python -m ingest.build_versioned --graph cod_versioned --per-year 260
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from turingdb import TuringDB, TuringDBException

DEFAULT_HOST = "http://localhost:6691"


def q(s: str | None) -> str:
    """Single-quote a string for inline Cypher (no parameters in this dialect)."""
    if s is None:
        return "''"
    return "'" + str(s).replace("\\", "").replace("'", "") + "'"


def _num(v: Any) -> float | None:
    try:
        if v is None or v == "" or str(v).upper() in ("NULL", "\\N", "NAN"):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    f = _num(v)
    return int(f) if f is not None else None


@dataclass
class YearCommit:
    year: int
    commit: str
    added: int
    total: int
    seconds: float
    components_added: int
    fragments_linked: int


@dataclass
class Struct:
    cod_id: int
    year: int
    hm: str
    sg_number: int
    system: str
    formula: str
    journal: str
    r_factor: float | None
    has_h: bool
    n_components: int
    n_contacts: int
    n_hbonds: int
    n_hbonds_located: int
    components: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------
# load the parsed corpus
# --------------------------------------------------------------------------


def load_corpus(cache: Path, meta_csv: Path, per_year: int | None) -> list[Struct]:
    meta: dict[int, dict[str, str]] = {}
    with meta_csv.open() as fh:
        for row in csv.DictReader(fh):
            try:
                meta[int(row["file"])] = row
            except (KeyError, ValueError):
                continue

    # The parse cache is append-only across resumed runs, so it can hold the
    # same structure more than once. Keep the last record per COD id.
    latest: dict[int, dict[str, Any]] = {}
    with cache.open() as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(r, dict) or r.get("skipped") or "atoms" not in r:
                continue
            latest[int(r["cod_id"])] = r

    by_year: dict[int, list[Struct]] = defaultdict(list)
    for cod_id, r in latest.items():
        m = meta.get(cod_id)
        if not m:
            continue
        year = _int(m.get("year"))
        if year is None or not (1900 < year < 2100):
            continue

        contacts = r.get("contacts", [])
        n_hb = sum(1 for c in contacts if c.get("kind") == "hbond")
        n_hb_loc = sum(
            1 for c in contacts if c.get("kind") == "hbond" and not c.get("h_inferred")
        )
        comps = []
        for c in r.get("components", []):
            key = c.get("inchikey") or c.get("fallback_key") or f"poly_{c.get('formula')}"
            comps.append({
                "key": key,
                "formula": c.get("formula") or "",
                "is_solvent": bool(c.get("is_solvent")),
                "has_inchikey": bool(c.get("inchikey")),
                "n_heavy": int(c.get("n_heavy") or 0),
                "role": c.get("role") or "other",
                "fragments": list(c.get("fragments") or []),
            })

        by_year[year].append(Struct(
            cod_id=cod_id,
            year=year,
            hm=r.get("spacegroup_hm") or "",
            sg_number=int(r.get("spacegroup_number") or 0),
            system=r.get("crystal_system") or "",
            formula=(m.get("formula") or "").strip("- ") or "",
            journal=m.get("_journal_key") or "",
            r_factor=_num(m.get("Robs")) or _num(m.get("Rall")) or _num(m.get("Rref")),
            has_h=any(a.get("element") == "H" for a in r.get("atoms", [])),
            n_components=len(comps),
            n_contacts=len(contacts),
            n_hbonds=n_hb,
            n_hbonds_located=n_hb_loc,
            components=comps,
        ))

    out: list[Struct] = []
    for year in sorted(by_year):
        rows = sorted(by_year[year], key=lambda s: s.cod_id)
        if per_year:
            # Even sample across the year rather than the first N, since COD ids
            # are chronological within a year too.
            if len(rows) > per_year:
                step = len(rows) / per_year
                rows = [rows[int(i * step)] for i in range(per_year)]
        out.extend(rows)
    return out


# --------------------------------------------------------------------------
# the chronological build
# --------------------------------------------------------------------------


def head_commit(client: TuringDB) -> str:
    hist = client.query("CALL db.history()")
    if not len(hist):
        return ""
    return str(hist.iloc[0, 0]).replace("(HEAD)", "").strip()


def ensure_graph(client: TuringDB, name: str) -> None:
    try:
        client.create_graph(name)
    except TuringDBException:
        pass
    try:
        client.query(f"LOAD GRAPH {name}")
    except Exception:  # noqa: BLE001
        pass
    client.set_graph(name)


def wipe(client: TuringDB) -> None:
    try:
        client.new_change()
        client.query("MATCH (n) DETACH DELETE n")
        client.query("CHANGE SUBMIT")
        client.checkout()
    except Exception as exc:  # noqa: BLE001
        print(f"  wipe skipped: {' '.join(str(exc).split())[:120]}")


def create_indexes(client: TuringDB) -> None:
    """Property indexes, so the per-structure edge MATCHes are lookups not scans.

    Without these the build is quadratic: every edge statement scans a graph
    that grows with each year.
    """
    stmts = [
        "CREATE INDEX ix_cod_id FOR (n) ON n.cod_id",
        "CREATE INDEX ix_comp_key FOR (n) ON n.component_key",
        "CREATE INDEX ix_frag FOR (n) ON n.fragment_type",
        "CREATE INDEX ix_hm FOR (n) ON n.hm_symbol",
    ]
    try:
        client.new_change()
        for s in stmts:
            try:
                client.query(s)
            except Exception as exc:  # noqa: BLE001
                print(f"  index skipped ({s.split()[2]}): "
                      f"{' '.join(str(exc).split())[:90]}")
        client.query("CHANGE SUBMIT")
        client.checkout()
        print("  property indexes created")
    except Exception as exc:  # noqa: BLE001
        print(f"  could not create indexes: {' '.join(str(exc).split())[:140]}")


def build(
    client: TuringDB,
    corpus: list[Struct],
    batch: int = 250,
) -> list[YearCommit]:
    by_year: dict[int, list[Struct]] = defaultdict(list)
    for s in corpus:
        by_year[s.year].append(s)

    seen_components: set[str] = set()
    seen_fragments: set[str] = set()
    seen_spacegroups: set[str] = set()
    ledger: list[YearCommit] = []
    total = 0

    for year in sorted(by_year):
        rows = by_year[year]
        t0 = time.time()
        client.new_change()

        # ---- nodes -----------------------------------------------------
        creates: list[str] = []
        for s in rows:
            props = [
                f"cod_id:{s.cod_id}", f"year:{s.year}",
                f"hm_symbol:{q(s.hm)}", f"crystal_system:{q(s.system)}",
                f"formula:{q(s.formula)}", f"journal_key:{q(s.journal)}",
                f"has_hydrogens:{'true' if s.has_h else 'false'}",
                f"n_components:{s.n_components}", f"n_contacts:{s.n_contacts}",
                f"n_hbonds:{s.n_hbonds}", f"n_hbonds_located:{s.n_hbonds_located}",
                f"has_r_factor:{'true' if s.r_factor is not None else 'false'}",
            ]
            if s.r_factor is not None:
                props.append(f"r_factor:{s.r_factor:.5f}")
            creates.append("CREATE (:Structure {" + ", ".join(props) + "})")

            if s.hm and s.hm not in seen_spacegroups:
                seen_spacegroups.add(s.hm)
                creates.append(
                    "CREATE (:SpaceGroup {"
                    f"hm_symbol:{q(s.hm)}, number:{s.sg_number}, "
                    f"crystal_system:{q(s.system)}" + "})"
                )
            for c in s.components:
                if c["key"] in seen_components:
                    continue
                seen_components.add(c["key"])
                creates.append(
                    "CREATE (:Component {"
                    f"component_key:{q(c['key'])}, formula:{q(c['formula'])}, "
                    f"is_solvent:{'true' if c['is_solvent'] else 'false'}, "
                    f"has_inchikey:{'true' if c['has_inchikey'] else 'false'}, "
                    f"n_heavy:{c['n_heavy']}" + "})"
                )
                for fr in c["fragments"]:
                    if fr in seen_fragments:
                        continue
                    seen_fragments.add(fr)
                    creates.append(
                        f"CREATE (:Fragment {{fragment_type:{q(fr)}}})"
                    )

        for i in range(0, len(creates), batch):
            client.query("\n".join(creates[i : i + batch]))

        # A MATCH cannot see CREATEs from the same change until they are
        # committed, so the edge statements below would match nothing.
        client.query("COMMIT")

        # ---- edges: one statement per structure --------------------------
        n_frag_links = 0
        linked_components: set[str] = set()
        for s in rows:
            matches = [f"(st:Structure {{cod_id:{s.cod_id}}})"]
            creates2: list[str] = []
            if s.hm:
                matches.append(f"(sg:SpaceGroup {{hm_symbol:{q(s.hm)}}})")
                creates2.append("(st)-[:IN_SPACE_GROUP]->(sg)")
            for i, c in enumerate(s.components):
                var = f"cp{i}"
                matches.append(f"({var}:Component {{component_key:{q(c['key'])}}})")
                creates2.append(
                    f"(st)-[:CONTAINS_COMPONENT {{role:{q(c['role'])}}}]->({var})"
                )
            if creates2:
                try:
                    client.query(
                        "MATCH " + ", ".join(matches) + " CREATE " + ", ".join(creates2)
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"    ! edges for {s.cod_id}: "
                          f"{' '.join(str(exc).split())[:110]}")

            # component -> fragment, once per component
            for c in s.components:
                if c["key"] in linked_components or not c["fragments"]:
                    continue
                linked_components.add(c["key"])
                fm = [f"(cx:Component {{component_key:{q(c['key'])}}})"]
                fc = []
                for j, fr in enumerate(c["fragments"]):
                    fm.append(f"(fg{j}:Fragment {{fragment_type:{q(fr)}}})")
                    fc.append(f"(cx)-[:HAS_FRAGMENT]->(fg{j})")
                try:
                    client.query("MATCH " + ", ".join(fm) + " CREATE " + ", ".join(fc))
                    n_frag_links += len(fc)
                except Exception as exc:  # noqa: BLE001
                    print(f"    ! fragments for {c['key']}: "
                          f"{' '.join(str(exc).split())[:110]}")

        client.query("CHANGE SUBMIT")
        client.checkout()

        total += len(rows)
        commit = head_commit(client)
        secs = time.time() - t0
        ledger.append(YearCommit(
            year=year, commit=commit, added=len(rows), total=total,
            seconds=round(secs, 1), components_added=len(seen_components),
            fragments_linked=n_frag_links,
        ))
        print(f"  cod@{year}  +{len(rows):>4} -> {total:>5} total   "
              f"{secs:>5.1f}s   {commit[:16]}", flush=True)

    return ledger


# --------------------------------------------------------------------------
# the tag ledger (a second, queryable graph)
# --------------------------------------------------------------------------


def write_ledger(client: TuringDB, ledger: list[YearCommit], graph: str,
                 data_graph: str) -> None:
    """Persist year -> commit as a graph, since TuringDB has no tags."""
    ensure_graph(client, graph)
    wipe(client)
    client.new_change()
    creates = []
    for row in ledger:
        creates.append(
            "CREATE (:Snapshot {"
            f"tag:{q(f'cod@{row.year}')}, year:{row.year}, "
            f"commit_hash:{q(row.commit)}, src_graph:{q(data_graph)}, "
            f"structures_added:{row.added}, structures_total:{row.total}, "
            f"components_total:{row.components_added}, "
            f"build_seconds:{row.seconds}" + "})"
        )
    client.query("\n".join(creates))
    client.query("COMMIT")
    # chain them so the log can be walked in order
    for a, b in zip(ledger, ledger[1:]):
        try:
            client.query(
                f"MATCH (x:Snapshot {{year:{a.year}}}), (y:Snapshot {{year:{b.year}}}) "
                "CREATE (x)-[:NEXT]->(y)"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  ! NEXT {a.year}->{b.year}: {' '.join(str(exc).split())[:90]}")
    client.query("CHANGE SUBMIT")
    client.checkout()
    print(f"  ledger graph '{graph}': {len(ledger)} snapshots")


# --------------------------------------------------------------------------
# verification -- the whole point is that time travel actually works
# --------------------------------------------------------------------------


def verify(client: TuringDB, ledger: list[YearCommit], graph: str) -> None:
    ensure_graph(client, graph)
    print("\n  time-travel check (structures visible at each tag):")
    print(f"    {'tag':<12}{'expected':>10}{'observed':>10}{'acid frags':>12}  ok")
    ok_all = True
    probes = [r for i, r in enumerate(ledger) if i % max(1, len(ledger) // 6) == 0]
    if ledger and ledger[-1] not in probes:
        probes.append(ledger[-1])
    for row in probes:
        try:
            client.checkout(commit=row.commit)
            n = int(client.query("MATCH (s:Structure) RETURN count(s)").iloc[0, 0])
            acid = int(client.query(
                "MATCH (c:Component)-[:HAS_FRAGMENT]->(f:Fragment) "
                "WHERE f.fragment_type = 'carboxylic_acid' RETURN count(c)"
            ).iloc[0, 0])
        except Exception as exc:  # noqa: BLE001
            print(f"    cod@{row.year:<8}{row.total:>10}  FAILED "
                  f"{' '.join(str(exc).split())[:70]}")
            ok_all = False
            continue
        good = n == row.total
        ok_all = ok_all and good
        print(f"    cod@{row.year:<8}{row.total:>10}{n:>10}{acid:>12}  "
              f"{'yes' if good else 'NO'}")
    client.checkout()
    print(f"\n  RESULT: {'time travel verified' if ok_all else 'MISMATCH - investigate'}")


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--graph", default="cod_versioned")
    ap.add_argument("--ledger-graph", default="cod_versions")
    ap.add_argument("--cache", type=Path,
                    default=Path("data/jsonl/cod_slice.cache.jsonl"))
    ap.add_argument("--metadata", type=Path,
                    default=Path("data/manifest/slice_metadata.csv"))
    ap.add_argument("--per-year", type=int, default=260,
                    help="cap structures per year (0 = no cap)")
    ap.add_argument("--fresh", action="store_true", help="wipe before building")
    args = ap.parse_args(argv)

    if not args.cache.exists():
        print(f"missing {args.cache}", file=sys.stderr)
        return 1

    print("loading parsed corpus ...")
    corpus = load_corpus(args.cache, args.metadata, args.per_year or None)
    years = sorted({s.year for s in corpus})
    print(f"  {len(corpus):,} structures across {len(years)} years "
          f"({years[0]}-{years[-1]})")

    client = TuringDB(host=args.host)
    ensure_graph(client, args.graph)
    if args.fresh:
        print("wiping ...")
        wipe(client)
    create_indexes(client)

    print(f"\nbuilding one commit per year into '{args.graph}' ...")
    t0 = time.time()
    ledger = build(client, corpus)
    print(f"  {len(ledger)} commits in {time.time() - t0:.0f}s")

    write_ledger(client, ledger, args.ledger_graph, args.graph)
    verify(client, ledger, args.graph)

    out = Path("data/manifest/versioned_ledger.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "graph": args.graph,
        "ledger_graph": args.ledger_graph,
        "per_year_cap": args.per_year,
        "snapshots": [vars(r) for r in ledger],
    }, indent=2) + "\n")
    print(f"  ledger json: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
