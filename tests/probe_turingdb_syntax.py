"""Probe TuringDB's OWN documented syntax (not Neo4j's) for the demo's needs.

probe_critical.py deliberately tests the Neo4j spellings from the project spec.
This file tests the forms docs.turingdb.ai actually documents, so we learn what
the demo CAN express rather than only what it can't:

  * variable length is a POSTFIX QUANTIFIER  -[e]->{2,4}(b)   not  -[e*2..4]->(b)
  * shortestPath is a bare STATEMENT with 5 args, not a pattern function
  * aggregates are count()/avg() only, and only as a SINGLE return item
  * property indexes exist and matter for seeded lookups

Usage:
    TURING_HOST=http://localhost:6691 ./.venv/bin/python tests/probe_turingdb_syntax.py
"""

from __future__ import annotations

import os
import sys

from turingdb import TuringDB, TuringDBException

HOST = os.environ.get("TURING_HOST", "http://localhost:6691")
GRAPH = os.environ.get("PROBE_GRAPH", "probe_critical")  # reuse the built fixture

# (group, label, cypher, why)
TESTS: list[tuple[str, str, str, str]] = [
    # ---------------------------------------------------------------- Q2 ---
    # The headline claim. Docs give postfix +, * and {m,n}.
    ("quantifier", "bare edge, one-or-more  -[e]->+",
     "MATCH (a:Atom)-[e]->+(b:Atom) RETURN count(b)", "Q2"),
    ("quantifier", "bare edge, zero-or-more  -[e]->*",
     "MATCH (a:Atom)-[e]->*(b:Atom) RETURN count(b)", "Q2"),
    ("quantifier", "bare edge, range  -[e]->{2,4}",
     "MATCH (a:Atom)-[e]->{2,4}(b:Atom) RETURN count(b)", "Q2"),
    ("quantifier", "TYPED edge + range  -[e:CONTACT]->{1,8}",
     "MATCH (a:Atom)-[e:CONTACT]->{1,8}(b:Atom) RETURN count(b)",
     "Q2 restricted to contacts - docs do NOT confirm this combination"),
    ("quantifier", "typed anon edge + range  -[:CONTACT]->{1,8}",
     "MATCH (a:Atom)-[:CONTACT]->{1,8}(b:Atom) RETURN count(b)", "Q2"),
    ("quantifier", "SEEDED typed + range (the real Q2)",
     "MATCH (a:Atom {uid:'A0'})-[e:CONTACT]->{1,8}(b:Atom) RETURN count(b)",
     "Q2 exactly as the demo needs it"),
    ("quantifier", "exact depth {8,8}",
     "MATCH (a:Atom {uid:'A0'})-[e:CONTACT]->{8,8}(b:Atom) RETURN count(b)",
     "Q2 growth curve, one query per depth"),
    ("quantifier", "exact depth {3}",
     "MATCH (a:Atom {uid:'A0'})-[e:CONTACT]->{3}(b:Atom) RETURN count(b)",
     "Q2 shorthand"),
    ("quantifier", "quantifier + WHERE on rel prop",
     "MATCH (a:Atom {uid:'A0'})-[e:CONTACT]->{1,8}(b:Atom) WHERE e.kind = 'hbond' "
     "RETURN count(b)", "Q2 hbond-only network"),
    ("quantifier", "quantifier + INLINE rel prop",
     "MATCH (a:Atom {uid:'A0'})-[e:CONTACT {kind:'hbond'}]->{1,8}(b:Atom) RETURN count(b)",
     "Q2 hbond-only network"),
    ("quantifier", "undirected quantifier  -[e]-{1,3}",
     "MATCH (a:Atom {uid:'A0'})-[e:CONTACT]-{1,3}(b:Atom) RETURN count(b)",
     "packing network ignores donor/acceptor direction"),
    ("quantifier", "reverse quantifier  <-[e]-{1,3}",
     "MATCH (a:Atom {uid:'A0'})<-[e:CONTACT]-{1,3}(b:Atom) RETURN count(b)", "Q2"),
    ("quantifier", "quantifier reaching 12 hops",
     "MATCH (a:Atom {uid:'A0'})-[e:CONTACT]->{1,12}(b:Atom) RETURN count(b)",
     "depth headroom beyond 8"),

    # ------------------------------------------------------------ shortest ---
    ("shortestpath", "shortestPath statement form",
     "MATCH (n:Atom {uid:'A0'}), (m:Atom {uid:'A5'}) "
     "shortestPath(n, m, length, dist, path) RETURN dist, path",
     "path between two sites"),

    # ---------------------------------------------------------- aggregates ---
    ("aggregate", "count(*) single item",
     "MATCH (a:Atom) RETURN count(*)", "baseline"),
    ("aggregate", "count + avg combined in ONE item",
     "MATCH ()-[e:CONTACT]->() RETURN count(e) + avg(e.length)",
     "docs say aggregates may be combined within a single return item"),
    ("aggregate", "avg alone", "MATCH ()-[e:CONTACT]->() RETURN avg(e.length)", "reporting"),
    ("aggregate", "count with WHERE = per-type frequency (Q4 workaround)",
     "MATCH (f:Fragment)<-[:HAS_FRAGMENT]-(c:Component) "
     "WHERE f.fragment_type = 'carboxylic_acid' RETURN count(c)",
     "Q4 motif table: one query per fragment type instead of GROUP BY"),
    ("aggregate", "count over a 4-hop join (Q4 shape)",
     "MATCH (fa:Fragment)<-[:HAS_FRAGMENT]-(c:Component)<-[:CONTAINS_COMPONENT]-(s:Structure) "
     "WHERE fa.fragment_type = 'carboxylic_acid' RETURN count(s)",
     "Q4 per-motif count"),

    # --------------------------------------------------------------- joins ---
    ("join", "comma-joined 3 patterns sharing vars",
     "MATCH (f:Fragment)<-[:HAS_FRAGMENT]-(c:Component), "
     "(c)<-[:CONTAINS_COMPONENT]-(s:Structure), (s)-[:HAS_SITE]->(a:Atom) "
     "RETURN s.cod_id, f.fragment_type, a.uid", "Q1/Q3 need multi-pattern joins"),
    ("join", "self-join on shared node with <>",
     "MATCH (c1:Component)-[:HAS_FRAGMENT]->(f:Fragment)<-[:HAS_FRAGMENT]-(c2:Component) "
     "WHERE c1.inchikey <> c2.inchikey RETURN c1.name, c2.name",
     "Q3 chemical-similarity step"),
    ("join", "Q3 full 5-hop chain, no aggregate",
     "MATCH (api:Component {inchikey:'KDY'})-[:HAS_FRAGMENT]->(f:Fragment)"
     "<-[:HAS_FRAGMENT]-(sim:Component), "
     "(sim)<-[:CONTAINS_COMPONENT]-(st:Structure), "
     "(st)-[:CONTAINS_COMPONENT]->(cand:Component) "
     "RETURN cand.inchikey, cand.name", "Q3 core traversal"),
    ("join", "Q1 cycle: two rels between the same pair",
     "MATCH (a1:Atom)-[h1:CONTACT]->(a2:Atom), (a2)-[h2:CONTACT]->(a1) "
     "RETURN a1.uid, a2.uid", "Q1 acid dimer - reciprocal pair"),
    ("join", "Q1 cycle via distinct endpoint vars",
     "MATCH (a1:Atom)-[h1:CONTACT]->(a2:Atom), (a3:Atom)-[h2:CONTACT]->(a4:Atom) "
     "WHERE a1.uid = a4.uid AND a2.uid = a3.uid RETURN a1.uid, a2.uid",
     "Q1 workaround if the direct cycle will not plan"),

    # -------------------------------------------------------------- indexes ---
    ("index", "CALL db.showIndexes()", "CALL db.showIndexes()", "seed-lookup speed"),
    ("index", "CALL db.procedures()", "CALL db.procedures()", "discover real procedure list"),

    # ------------------------------------------------------- introspection ---
    ("introspection", "CALL db.labels()", "CALL db.labels()", "schema check"),
    ("introspection", "CALL db.edgeTypes()", "CALL db.edgeTypes()", "schema check"),
    ("introspection", "CALL db.propertyTypes()", "CALL db.propertyTypes()", "schema check"),
    ("introspection", "CALL db.history()", "CALL db.history()", "Stage 4 commit log"),
    ("introspection", "CALL ... YIELD ... RETURN *",
     "CALL db.propertyTypes() YIELD propertyType, valueType RETURN *", "docs example"),
    ("introspection", "labels(n) + edgeType(e)",
     "MATCH (n)-[e]->(m) RETURN labels(n), edgeType(e), labels(m) LIMIT 5",
     "UI node/edge typing"),
    ("introspection", "WHERE n = <internal id>",
     "MATCH (n) WHERE n = 1 RETURN labels(n)", "seed by internal id"),
    ("introspection", "CHANGE LIST", "CHANGE LIST", "Stage 4"),

    # --------------------------------------------------------------- misc ----
    ("misc", "RETURN with AS alias",
     "MATCH (s:Structure) RETURN s.cod_id AS cod_id", "API payload shaping"),
    ("misc", "ORDER BY projected property",
     "MATCH ()-[e:CONTACT]->() RETURN e.length ORDER BY e.length LIMIT 5", "Q1 ordering"),
    ("misc", "arithmetic in RETURN",
     "MATCH (s:Structure) RETURN s.r_factor * 100.0", "reporting"),
    ("misc", "IS NULL",
     "MATCH (s:Structure) WHERE s.pressure IS NULL RETURN s.cod_id",
     "COD missing-field handling"),
    ("misc", "IS NOT NULL",
     "MATCH (s:Structure) WHERE s.temperature IS NOT NULL RETURN s.cod_id",
     "COD missing-field handling"),
    ("misc", "list literal RETURN",
     "RETURN [1,2,3] AS nums", "docs say literal elements only"),
]


def main() -> int:
    print(f"connecting to {HOST}, graph={GRAPH}")
    client = TuringDB(host=HOST)
    try:
        client.query(f"LOAD GRAPH {GRAPH}")
    except Exception:  # noqa: BLE001
        pass
    client.set_graph(GRAPH)
    n = client.query("MATCH (n) RETURN count(n)").iloc[0, 0]
    print(f"fixture has {n} nodes\n")

    width = max(len(t[1]) for t in TESTS)
    passes: list[tuple[str, str, str]] = []
    fails: list[tuple[str, str, str, str]] = []
    current = ""
    for group, label, cypher, why in TESTS:
        if group != current:
            print(f"\n--- {group} ---")
            current = group
        try:
            df = client.query(cypher)
        except Exception as exc:  # noqa: BLE001
            msg = " ".join(str(exc).split())
            print(f"  FAIL  {label:<{width}}  {msg[:110]}")
            fails.append((group, label, why, msg[:300]))
        else:
            val = ""
            try:
                if len(df) and len(df.columns) == 1:
                    val = f"  = {df.iloc[0, 0]}"
                elif len(df):
                    val = f"  first={list(df.iloc[0])[:4]}"
            except Exception:  # noqa: BLE001
                pass
            print(f"  PASS  {label:<{width}}  rows={len(df)}{val}")
            passes.append((group, label, why))

    print(f"\n{'=' * 70}\n{len(passes)}/{len(TESTS)} supported")
    if fails:
        print("\nUNSUPPORTED:")
        for group, label, why, msg in fails:
            print(f"  [{group}] {label}")
            print(f"      needed for: {why}")
            print(f"      error     : {msg[:180]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
