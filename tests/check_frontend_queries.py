"""Execute every Cypher string the frontend ships, against a live server.

The failure mode this exists to catch is specific to this dialect and this app:
a projection of an absent property is an `ANALYZE_ERROR`, not an empty column,
and the UI wraps several queries in a `.catch(() => [])`. So a query that broke
because a property was renamed, or because a graph was rebuilt without it,
renders as **an empty canvas rather than an error** -- and an empty canvas reads
as "no data" to everyone in the room.

That has already bitten this family of demos more than once: `type(r)` became a
PARSE_ERROR in 1.36 and a swallowed error drew nodes with no links; here,
`IN_COMPONENT` and `net_dim` exist only in the v2 corpus, so four of the shipped
investigations would silently return nothing against an older graph.

The check is deliberately dumb: pull every template literal and string that
looks like Cypher out of the crystal module, run it, and require a non-error
response. Negative-control it by injecting a known-bad query before trusting a
green run.

Usage:
    .venv/bin/python tests/check_frontend_queries.py --graph cod_slice_v2
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

CRYSTAL = Path("visualizer/src/components/viewer/crystal")

#: A string is treated as Cypher only if it STARTS a query. `RETURN` and
#: `WHERE` are deliberately absent: they occur mid-string in concatenated
#: fragments and in prose, and including them made this check report its own
#: parsing as failures rather than the app's.
STARTS = ("MATCH ", "MATCH(", "CALL ", "LOAD ")

#: Anything containing one of these is JavaScript that merely begins with a
#: Cypher keyword -- a concatenation, a JSX block, an arrow function.
NOT_CYPHER = ('")', "\")", "=>", "<div", "</", "${", "` +", "return {",
              "'<")   # "'<" is a doc placeholder like LOAD COMMIT '<hash>'

#: Placeholders the UI substitutes at runtime. Replaced with something valid so
#: the query shape can still be executed.
SUBS = {
    r"\$\{PACKING_SEED_COD\}": "2229029",
    r"\$\{DIMER_COUNT\}": "",
    r"\$\{cod\}": "2229029",
    r"\$\{base\}": "",
    r"\$\{hm\}": "P -1",
    r"\$\{t\}": "carboxylic_acid",
    r"\$\{id\}": "0",
    r"\$\{proj\}": "n.cod_id",
    r"\$\{year\}": "2011",
    r"\$\{k\}": "2",
}


def extract(paths: list[Path]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for p in paths:
        text = p.read_text()
        # backtick template literals and ordinary quoted strings
        for m in re.finditer(r"`([^`]*)`|'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"", text):
            raw = m.group(1) or m.group(2) or m.group(3) or ""
            raw = raw.replace("\\'", "'").replace('\\"', '"')
            body = raw.strip()
            if not body.upper().startswith(STARTS):
                continue
            if any(tok in body for tok in NOT_CYPHER):
                continue
            # A candidate ending in a comma or an operator is one half of a
            # concatenated literal -- the other half is the next string in the
            # source. Running the fragment alone is a PARSE_ERROR that says
            # nothing about the app, so skip it rather than cry wolf.
            if body.rstrip().endswith((",", "+", "(", "AND", "OR", "WHERE")):
                continue
            for pat, rep in SUBS.items():
                body = re.sub(pat, rep, body)
            if "${" in body:          # an interpolation we do not know: skip
                continue
            line = text[: m.start()].count("\n") + 1
            out.append((f"{p.name}:{line}", " ".join(body.split())))
    return out


def _count_entries(text: str, i: int) -> int:
    """Count top-level entries of an array literal whose `[` is just before `i`.

    Written as an explicit scanner rather than a regex because the entries are
    `scalar(g, `...${x}...`)` calls: they nest parentheses, contain commas
    inside both ordinary strings and template literals, and template literals
    contain `${ }` holes that themselves nest. Anything less careful
    miscounts, which would make this check worse than useless -- it would
    report mismatches that are not real and train the reader to ignore it.
    """
    depth = 1
    entries = 0
    saw_content = False
    last_meaningful = ""
    n = len(text)
    while i < n and depth > 0:
        ch = text[i]
        if ch in "'\"":                       # ordinary string
            q, i = ch, i + 1
            while i < n and text[i] != q:
                i += 2 if text[i] == "\\" else 1
            saw_content, last_meaningful = True, "x"
        elif ch == "`":                        # template literal, may nest ${}
            i += 1
            hole = 0
            while i < n:
                c = text[i]
                if c == "\\":
                    i += 2
                    continue
                if c == "$" and i + 1 < n and text[i + 1] == "{":
                    hole += 1
                    i += 2
                    continue
                if c == "}" and hole:
                    hole -= 1
                elif c == "`" and not hole:
                    break
                i += 1
            saw_content, last_meaningful = True, "x"
        elif ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":  # line comment
                i += 1
            continue
        elif ch in "([{":
            depth += 1
            saw_content, last_meaningful = True, "x"
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                break
            saw_content, last_meaningful = True, "x"
        elif ch == "," and depth == 1:
            entries += 1
            last_meaningful = ","
        elif not ch.isspace():
            saw_content, last_meaningful = True, "x"
        i += 1
    if not saw_content:
        return 0
    # a trailing comma before `]` means entries == item count, else count - 1
    return entries if last_meaningful == "," else entries + 1


def check_destructure_arity(paths: list[Path]) -> list[str]:
    """`const [a, b, c] = await Promise.all([...])` must name exactly as many
    variables as the array has entries.

    This is not a style check. The panels read their numbers POSITIONALLY, so
    inserting one query into the wrong Promise.all shifts every variable after
    it: a count of structures lands in the variable that prints as
    "carboxylate components", and the panel renders a plausible, wrong number
    with no error at all. The only symptom may be a crash in a *different*
    panel whose array has gone one short.
    """
    bad: list[str] = []
    pat = re.compile(r"const\s*\[([^\]]*)\]\s*=\s*await\s+Promise\.all\(\[", re.S)
    for p in paths:
        text = p.read_text()
        for m in pat.finditer(text):
            names = [v.strip() for v in m.group(1).split(",") if v.strip()]
            count = _count_entries(text, m.end())
            if count != len(names):
                line = text[: m.start()].count("\n") + 1
                bad.append(
                    f"{p.name}:{line}  destructures {len(names)} "
                    f"({', '.join(names)}) from a Promise.all of {count}")
    return bad


def run(host: str, graph: str, cypher: str) -> str:
    req = urllib.request.Request(
        f"{host}/query?graph={graph}",
        data=cypher.encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            payload = json.loads(r.read())
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"[:120]
    # Failures arrive as HTTP 200 with a non-null top-level `error`.
    if payload.get("error"):
        return f"{payload['error']}: {payload.get('error_details', '')}"[:140]
    return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="http://localhost:6691")
    ap.add_argument("--graph", default="cod_slice_v2")
    ap.add_argument("--ledger-graph", default="cod_versions")
    ap.add_argument("--versioned-graph", default="cod_versioned")
    ap.add_argument("--negative-control", action="store_true",
                    help="inject a known-bad query and require that it FAILS")
    a = ap.parse_args(argv)

    files = sorted(CRYSTAL.glob("*.ts")) + sorted(CRYSTAL.glob("*.tsx"))
    if not files:
        print(f"no frontend sources under {CRYSTAL}", file=sys.stderr)
        return 1
    arity = check_destructure_arity(files)
    if arity:
        print("DESTRUCTURE / Promise.all ARITY MISMATCH -- panels read these "
              "positionally, so this renders wrong numbers, not an error:")
        for a_ in arity:
            print(f"  {a_}")
        return 1

    queries = extract(files)

    if a.negative_control:
        queries.append(("NEGATIVE-CONTROL",
                        "MATCH (n:Structure) RETURN n.property_that_does_not_exist"))

    # Snapshot queries belong to the ledger graph, Snapshot-free ones to the
    # corpus. Route by the labels they mention rather than by guessing.
    def graph_for(cypher: str) -> str:
        if "Snapshot" in cypher:
            return a.ledger_graph
        return a.graph

    bad: list[tuple[str, str, str]] = []
    missing: dict[str, int] = {}
    seen: set[str] = set()
    n = 0
    for where, cypher in queries:
        if cypher in seen:
            continue
        seen.add(cypher)
        n += 1
        target = graph_for(cypher)
        err = run(a.host, target, cypher)
        # A graph that is absent entirely (mid-rebuild, or never built on this
        # server) is reported separately: it is a deployment fact, not a broken
        # query, and conflating the two makes the check cry wolf.
        if err.startswith("GRAPH_NOT_FOUND"):
            missing.setdefault(target, 0)
            missing[target] += 1
            continue
        if err:
            bad.append((where, cypher, err))

    print(f"{n} distinct Cypher strings from {len(files)} frontend files")
    if a.negative_control:
        ctrl = [b for b in bad if b[0] == "NEGATIVE-CONTROL"]
        if not ctrl:
            print("NEGATIVE CONTROL PASSED WHEN IT SHOULD HAVE FAILED -- "
                  "this check is not actually executing anything", file=sys.stderr)
            return 2
        print("negative control failed as required (the check works)")
        bad = [b for b in bad if b[0] != "NEGATIVE-CONTROL"]

    for g, k in sorted(missing.items()):
        print(f"  NOTE: {k} queries skipped -- graph '{g}' is not loaded")
    if not bad:
        print("all shipped queries execute cleanly")
        return 0
    print(f"\n{len(bad)} FAILING:")
    for where, cypher, err in bad:
        print(f"\n  {where}\n    {cypher[:170]}\n    -> {err}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
