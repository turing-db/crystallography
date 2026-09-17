#!/usr/bin/env python3
"""Command-line access to the crystallography graph.

Everything the browser studio can ask, this can ask too. The studio is better
for looking at a packing motif; this is better for a corpus-wide number you
want to paste into a paper, a loop over many structures, or a quick check that
a criterion does what you think it does.

    crystal facts                        corpus summary, all figures re-derived
    crystal list                         the named queries
    crystal run dimer                    run one by name
    crystal run dimer --cypher           print its Cypher without running it
    crystal query "MATCH (s:Structure) RETURN count(s)"
    crystal query "..." --csv > out.csv
    crystal at 2009 "MATCH (s:Structure) RETURN count(s)"
    crystal commits                      the version ledger

Reads TURING_HOST (default http://localhost:6691) and TURING_GRAPH
(default cod_slice_v2).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_HOST = os.environ.get("TURING_HOST", "http://localhost:6691")
DEFAULT_GRAPH = os.environ.get("TURING_GRAPH", "cod_slice_v2")
VERSIONED_GRAPH = "cod_versioned"
LEDGER_GRAPH = "cod_versions"


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------


class QueryError(RuntimeError):
    pass


def query(cypher: str, graph: str = DEFAULT_GRAPH, host: str = DEFAULT_HOST,
          commit: str | None = None) -> tuple[list[str], list[list]]:
    """Run one Cypher statement. Returns (column names, rows).

    TuringDB answers column-oriented and in chunks: the payload is
    `{header: {column_names, column_types}, data: [chunk, ...]}` where each
    chunk is a list of COLUMNS, not rows. Failures arrive with HTTP 200 and a
    non-null top-level `error`, so the status code alone is not a check.
    """
    url = f"{host}/query?graph={urllib.parse.quote(graph)}"
    if commit:
        url += f"&commit={urllib.parse.quote(commit)}"
    req = urllib.request.Request(
        url, data=cypher.encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            payload = json.loads(r.read())
    except urllib.error.URLError as exc:
        raise QueryError(
            f"cannot reach TuringDB at {host} ({exc.reason}). "
            f"Is the server running? See README, 'Running it'.") from None
    if payload.get("error"):
        detail = payload.get("error_details") or ""
        raise QueryError(f"{payload['error']}: {detail}".strip().rstrip(":"))

    names: list[str] = payload.get("header", {}).get("column_names", [])
    cols: list[list] = []
    for chunk in payload.get("data") or []:
        if not isinstance(chunk, list):
            continue
        for i, col in enumerate(chunk):
            if not isinstance(col, list):
                continue
            while len(cols) <= i:
                cols.append([])
            cols[i].extend(col)
    n = len(cols[0]) if cols else 0
    rows = [[cols[c][i] if c < len(cols) else None for c in range(len(names))]
            for i in range(n)]
    return names, rows


def scalar(cypher: str, **kw) -> float | int | str | None:
    _, rows = query(cypher, **kw)
    return rows[0][0] if rows and rows[0] else None


# --------------------------------------------------------------------------
# the named queries
# --------------------------------------------------------------------------

#: Named queries, each a (description, cypher) pair. These are the same
#: questions the studio offers; `crystal run <name> --cypher` prints the Cypher
#: so it can be copied, edited and re-run with `crystal query`.
#: Named queries. Each is (description, cypher, tally-by).
#:
#: `tally-by` is not decoration. This Cypher dialect has no GROUP BY, and an
#: aggregate cannot be combined with another return item -- `RETURN
#: f.fragment_type, count(c)` is rejected, and so is an aggregate inside
#: ORDER BY. So a "how many of each" question is expressed as a FLAT
#: projection and counted client-side. When tally-by is set, the CLI groups on
#: that column and prints counts; `--cypher` still shows exactly the query that
#: ran, so nothing is hidden.
LIBRARY: dict[str, tuple[str, str, str | None]] = {
    "spacegroups": (
        "Structures per space group, most common first",
        "MATCH (s:Structure)-[:IN_SPACE_GROUP]->(g:SpaceGroup) "
        "RETURN g.hm_symbol",
        "g.hm_symbol",
    ),
    "centrosymmetric": (
        "How many structures are in a centrosymmetric space group",
        "MATCH (s:Structure)-[:IN_SPACE_GROUP]->(g:SpaceGroup) "
        "WHERE g.is_centrosymmetric = true RETURN count(s)",
        None,
    ),
    "contacts": (
        "The contact populations, counted by kind",
        "MATCH ()-[r:CONTACT]->() RETURN r.kind",
        "r.kind",
    ),
    "hbond-geometry": (
        "Mean H...A over located-hydrogen bonds only",
        "MATCH ()-[r:CONTACT]->() WHERE r.kind = 'hbond' "
        "AND r.h_inferred = false RETURN avg(r.length)",
        None,
    ),
    "dimensionality": (
        "Periodic-net dimensionality of the hydrogen-bond net",
        "MATCH (s:Structure) WHERE s.has_net_dim = true RETURN s.net_dim",
        "s.net_dim",
    ),
    "dimer": (
        "R2,2(8) carboxylic-acid dimers (involution-generated)",
        "MATCH (f1:Fragment)<-[:HAS_FRAGMENT]-(c1:Component)"
        "<-[:IN_COMPONENT]-(a1:Atom)-[h:CONTACT]->(a2:Atom)"
        "-[:IN_COMPONENT]->(c2:Component)-[:HAS_FRAGMENT]->(f2:Fragment) "
        "WHERE f1.fragment_type = 'carboxylic_acid' "
        "AND f2.fragment_type = 'carboxylic_acid' "
        "AND h.kind = 'hbond' AND h.h_inferred = false "
        "AND h.is_involution = true "
        "AND a1.element = 'O' AND a2.element = 'O' "
        "AND a1.cod_id = a2.cod_id "
        "RETURN a1.cod_id, a1.label, a2.label, h.symop_triplet, h.length, h.angle",
        None,
    ),
    "catemer": (
        "The same acid-acid bonds generated by a screw or glide: C(4) chains",
        "MATCH (f1:Fragment)<-[:HAS_FRAGMENT]-(c1:Component)"
        "<-[:IN_COMPONENT]-(a1:Atom)-[h:CONTACT]->(a2:Atom)"
        "-[:IN_COMPONENT]->(c2:Component)-[:HAS_FRAGMENT]->(f2:Fragment) "
        "WHERE f1.fragment_type = 'carboxylic_acid' "
        "AND f2.fragment_type = 'carboxylic_acid' "
        "AND h.kind = 'hbond' AND h.h_inferred = false "
        "AND h.is_involution = false "
        "AND a1.element = 'O' AND a2.element = 'O' "
        "AND a1.cod_id = a2.cod_id "
        "RETURN a1.cod_id, h.symop_triplet, h.length, h.angle",
        None,
    ),
    "synthon-homo": (
        "Acid-to-acid O-H...O bonds (the homosynthon)",
        "MATCH (f1:Fragment)<-[:HAS_FRAGMENT]-(c1:Component)"
        "<-[:IN_COMPONENT]-(a1:Atom)-[h:CONTACT]->(a2:Atom)"
        "-[:IN_COMPONENT]->(c2:Component)-[:HAS_FRAGMENT]->(f2:Fragment) "
        "WHERE f1.fragment_type = 'carboxylic_acid' "
        "AND f2.fragment_type = 'carboxylic_acid' "
        "AND h.kind = 'hbond' AND h.h_inferred = false "
        "AND a1.element = 'O' AND a2.element = 'O' RETURN count(h)",
        None,
    ),
    "synthon-hetero": (
        "Acid-to-pyridine O-H...N bonds (the heterosynthon)",
        "MATCH (f1:Fragment)<-[:HAS_FRAGMENT]-(c1:Component)"
        "<-[:IN_COMPONENT]-(a1:Atom)-[h:CONTACT]->(a2:Atom)"
        "-[:IN_COMPONENT]->(c2:Component)-[:HAS_FRAGMENT]->(f2:Fragment) "
        "WHERE f1.fragment_type = 'carboxylic_acid' "
        "AND f2.fragment_type = 'pyridine_nitrogen' "
        "AND h.kind = 'hbond' AND h.h_inferred = false "
        "AND a1.element = 'O' AND a2.element = 'N' RETURN count(h)",
        None,
    ),
    "fragments": (
        "How many distinct molecules carry each perceived functional group",
        "MATCH (c:Component)-[:HAS_FRAGMENT]->(f:Fragment) "
        "RETURN f.fragment_type",
        "f.fragment_type",
    ),
    "packing": (
        "Every stored contact of one structure, with its symmetry operation",
        "MATCH (a:Atom)-[r:CONTACT]->(b:Atom) WHERE a.cod_id = 2229029 "
        "RETURN a.label, b.label, r.kind, r.symop, r.length, r.angle",
        None,
    ),
    "solvates": (
        "Which solvents appear, and in how many structures",
        "MATCH (s:Structure)-[:CONTAINS_COMPONENT]->(c:Component) "
        "WHERE c.is_solvent = true RETURN c.formula",
        "c.formula",
    ),
    "recurrence": (
        "Molecules appearing in the most structures",
        "MATCH (s:Structure)-[:CONTAINS_COMPONENT]->(c:Component) "
        "WHERE c.is_solvent = false AND c.has_inchikey = true "
        "RETURN c.inchikey",
        "c.inchikey",
    ),
}


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------


#: Columns whose integers are IDENTIFIERS, not quantities. A COD id printed as
#: "2,206,537" cannot be pasted into a search box or a script, so digit
#: grouping is applied to counts and never to these.
_ID_COLUMN = ("cod_id", "id", "number", "year", "atomic_number")


def is_id_column(name: str) -> bool:
    leaf = name.rsplit(".", 1)[-1].strip("`")
    return leaf in _ID_COLUMN


def fmt(v: object, *, group: bool = True) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:.4f}".rstrip("0").rstrip(".")
    if isinstance(v, int):
        return f"{v:,}" if group else str(v)
    return str(v)


def print_table(names: list[str], rows: list[list], limit: int | None = None) -> None:
    if not names:
        print("(no columns)")
        return
    shown = rows[:limit] if limit else rows
    group = [not is_id_column(n) for n in names]
    cells = [[fmt(v, group=group[i] if i < len(group) else True)
              for i, v in enumerate(r)] for r in shown]
    widths = [len(n) for n in names]
    for r in cells:
        for i, c in enumerate(r):
            if i < len(widths):
                widths[i] = max(widths[i], len(c))
    print("  ".join(n.ljust(widths[i]) for i, n in enumerate(names)))
    print("  ".join("-" * w for w in widths))
    for r in cells:
        print("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)))
    if limit and len(rows) > limit:
        print(f"... {len(rows) - limit:,} more rows (use --limit 0 for all)")
    print(f"\n{len(rows):,} row(s)")


def print_csv(names: list[str], rows: list[list]) -> None:
    w = csv.writer(sys.stdout)
    w.writerow(names)
    for r in rows:
        w.writerow(["" if v is None else v for v in r])


def tally_rows(names: list[str], rows: list[list],
               column: str) -> tuple[list[str], list[list]]:
    """Count rows by one column, most frequent first.

    The dialect has no GROUP BY, so grouping happens here. Doing it in Python
    over a flat projection is exact -- it is the same rows the server would
    have grouped -- and it keeps the shipped Cypher something you can paste
    into any other client.
    """
    from collections import Counter

    idx = names.index(column) if column in names else 0
    counts = Counter("-" if r[idx] is None else r[idx] for r in rows)
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
    return [column, "count"], [[k, v] for k, v in ordered]


def emit(names, rows, args) -> None:
    if args.json:
        json.dump([dict(zip(names, r)) for r in rows], sys.stdout, indent=2, default=str)
        print()
    elif args.csv:
        print_csv(names, rows)
    else:
        print_table(names, rows, None if args.limit == 0 else args.limit)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_facts(args) -> int:
    g, h = args.graph, args.host
    q = lambda c: scalar(c, graph=g, host=h)  # noqa: E731
    print(f"graph: {g}   host: {h}\n")
    rows = [
        ("structures", q("MATCH (s:Structure) RETURN count(s)")),
        ("atomic sites", q("MATCH (n:Atom) RETURN count(n)")),
        ("components", q("MATCH (n:Component) RETURN count(n)")),
        ("covalent bonds", q("MATCH ()-[r:BONDED_TO]->() RETURN count(r)")),
        ("space groups", q("MATCH (n:SpaceGroup) RETURN count(n)")),
    ]
    for k, v in rows:
        print(f"  {k:<26}{fmt(v):>14}")

    print("\ncontact layer (three populations, never blended):")
    pops = [
        ("strong H-bond (H located)",
         "MATCH ()-[r:CONTACT]->() WHERE r.kind = 'hbond' AND r.h_inferred = false RETURN count(r)"),
        ("inferred (no H refined)",
         "MATCH ()-[r:CONTACT]->() WHERE r.kind = 'hbond' AND r.h_inferred = true RETURN count(r)"),
        ("weak C-H...A",
         "MATCH ()-[r:CONTACT]->() WHERE r.kind = 'hbond_weak' RETURN count(r)"),
        ("halogen bond",
         "MATCH ()-[r:CONTACT]->() WHERE r.kind = 'halogen' RETURN count(r)"),
        ("close_contact (excluded)",
         "MATCH ()-[r:CONTACT]->() WHERE r.kind = 'close_contact' RETURN count(r)"),
    ]
    for k, c in pops:
        print(f"  {k:<26}{fmt(q(c)):>14}")

    ha = q("MATCH ()-[r:CONTACT]->() WHERE r.kind = 'hbond' AND r.h_inferred = false RETURN avg(r.length)")
    an = q("MATCH ()-[r:CONTACT]->() WHERE r.kind = 'hbond' AND r.h_inferred = false RETURN avg(r.angle)")
    print(f"\n  mean H...A {float(ha):.3f} A at {float(an):.2f} deg "
          f"(strong, located-H population only)")

    total = q("MATCH (s:Structure) RETURN count(s)")
    centro = q("MATCH (s:Structure)-[:IN_SPACE_GROUP]->(g:SpaceGroup) "
               "WHERE g.is_centrosymmetric = true RETURN count(s)")
    print(f"  centrosymmetric {100 * float(centro) / float(total):.1f}% of structures")

    print("\nperiodic-net dimensionality (h_inferred excluded):")
    lab = {0: "0D finite motif", 1: "1D chain", 2: "2D sheet", 3: "3D framework"}
    scored = sum(float(q(f"MATCH (s:Structure) WHERE s.net_dim = {k} RETURN count(s)"))
                 for k in range(4))
    for k in range(4):
        n = float(q(f"MATCH (s:Structure) WHERE s.net_dim = {k} RETURN count(s)"))
        print(f"  {lab[k]:<26}{fmt(int(n)):>10}  {100 * n / max(scored, 1):>5.1f}%")
    print(f"  {'scored':<26}{fmt(int(scored)):>10}")
    return 0


def cmd_list(args) -> int:
    width = max(len(k) for k in LIBRARY)
    for name, (desc, _c, _t) in LIBRARY.items():
        print(f"  {name.ljust(width)}  {desc}")
    print(f"\n{len(LIBRARY)} named queries. "
          f"`crystal run <name> --cypher` prints the Cypher.")
    return 0


def cmd_run(args) -> int:
    if args.name not in LIBRARY:
        near = [k for k in LIBRARY if args.name in k]
        print(f"no query named '{args.name}'."
              + (f" Did you mean: {', '.join(near)}?" if near else
                 " Run `crystal list`."), file=sys.stderr)
        return 2
    desc, cypher, tally = LIBRARY[args.name]
    if args.cypher:
        print(cypher)
        return 0
    if not (args.json or args.csv):
        print(f"{desc}\n")
    names, rows = query(cypher, graph=args.graph, host=args.host)
    if tally:
        names, rows = tally_rows(names, rows, tally)
    emit(names, rows, args)
    return 0


def cmd_query(args) -> int:
    names, rows = query(args.cypher, graph=args.graph, host=args.host)
    emit(names, rows, args)
    return 0


def cmd_at(args) -> int:
    """Run a query against the corpus as it stood at the end of one year."""
    _, ledger = query(
        "MATCH (s:Snapshot) RETURN s.year, s.commit_hash ORDER BY s.year",
        graph=LEDGER_GRAPH, host=args.host)
    hashes = {int(y): h for y, h in ledger}
    if args.year not in hashes:
        print(f"no commit for {args.year}. Available: "
              f"{min(hashes)}-{max(hashes)}", file=sys.stderr)
        return 2
    commit = hashes[args.year]
    # A commit must be resident before it can be read; this is idempotent.
    query(f"LOAD COMMIT '{commit}'", graph=VERSIONED_GRAPH, host=args.host)
    if not (args.json or args.csv):
        print(f"cod@{args.year}  commit {commit}\n")
    names, rows = query(args.cypher, graph=VERSIONED_GRAPH,
                        host=args.host, commit=commit)
    emit(names, rows, args)
    return 0


def cmd_commits(args) -> int:
    names, rows = query(
        "MATCH (s:Snapshot) RETURN s.year, s.tag, s.structures_total, "
        "s.structures_added, s.commit_hash ORDER BY s.year",
        graph=LEDGER_GRAPH, host=args.host)
    emit(names, rows, args)
    return 0


def cmd_graphs(args) -> int:
    names, rows = query("LIST AVAILABLE GRAPHS", graph="default", host=args.host)
    emit(names, rows, args)
    return 0


def main(argv: list[str] | None = None) -> int:
    def add_common(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
        """Common flags, accepted either before OR after the subcommand.

        `crystal run dimer --limit 5` is the obvious thing to type, so it has
        to work as well as `crystal --limit 5 run dimer`. The subcommand copies
        default to SUPPRESS, so they only appear in the namespace when actually
        given and therefore never clobber a value set before the subcommand.
        """
        d = (lambda v: argparse.SUPPRESS) if suppress else (lambda v: v)
        parser.add_argument("--host", default=d(DEFAULT_HOST),
                            help=f"TuringDB REST endpoint (default {DEFAULT_HOST})")
        parser.add_argument("--graph", default=d(DEFAULT_GRAPH),
                            help=f"graph to query (default {DEFAULT_GRAPH})")
        parser.add_argument("--csv", action="store_true",
                            default=d(False), help="CSV to stdout")
        parser.add_argument("--json", action="store_true",
                            default=d(False), help="JSON to stdout")
        parser.add_argument("--limit", type=int, default=d(40),
                            help="rows to print, 0 for all (default 40)")

    ap = argparse.ArgumentParser(
        prog="crystal", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common(ap, suppress=False)

    common = argparse.ArgumentParser(add_help=False)
    add_common(common, suppress=True)

    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("facts", parents=[common],
                   help="corpus summary").set_defaults(fn=cmd_facts)
    sub.add_parser("list", parents=[common],
                   help="the named queries").set_defaults(fn=cmd_list)
    sub.add_parser("commits", parents=[common],
                   help="the version ledger").set_defaults(fn=cmd_commits)
    sub.add_parser("graphs", parents=[common],
                   help="graphs on the server").set_defaults(fn=cmd_graphs)

    p = sub.add_parser("run", parents=[common], help="run a named query")
    p.add_argument("name")
    p.add_argument("--cypher", action="store_true", help="print the Cypher, do not run")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("query", parents=[common], help="run your own Cypher")
    p.add_argument("cypher")
    p.set_defaults(fn=cmd_query)

    p = sub.add_parser("at", parents=[common],
                       help="run Cypher against the corpus as of a year")
    p.add_argument("year", type=int)
    p.add_argument("cypher")
    p.set_defaults(fn=cmd_at)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except QueryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
