"""Fast, focused probe of the Cypher constructs the demo cannot do without.

Separate from probe_dialect.py so it can be pointed at a candidate server build
in a few seconds. Builds a 12-atom hydrogen-bond chain and asks: can this engine
express the four demo queries at all?

Usage:
    TURING_HOST=http://localhost:6691 ./.venv/bin/python tests/probe_critical.py
"""

from __future__ import annotations

import os
import sys

from turingdb import TuringDB, TuringDBException

HOST = os.environ.get("TURING_HOST", "http://localhost:6691")
GRAPH = os.environ.get("PROBE_GRAPH", "probe_critical")
N_CHAIN = 12

# (label, cypher, why it matters)
CRITICAL: list[tuple[str, str, str]] = [
    # --- Q2, the headline "deep multi-hop traversal" claim -----------------
    ("var-len *1..3",
     "MATCH (a:Atom {uid:'A0'})-[:CONTACT*1..3]->(b:Atom) RETURN count(b)",
     "Q2 as specified"),
    ("var-len *1..8",
     "MATCH (a:Atom {uid:'A0'})-[:CONTACT*1..8]->(b:Atom) RETURN count(b)",
     "Q2 at full depth"),
    ("var-len + inline rel prop",
     "MATCH (a:Atom {uid:'A0'})-[:CONTACT*1..8 {kind:'hbond'}]->(b:Atom) RETURN count(b)",
     "Q2 restricted to hydrogen bonds"),
    ("named path + length()",
     "MATCH p = (a:Atom {uid:'A0'})-[:CONTACT*1..4]->(b:Atom) RETURN length(p), count(b)",
     "Q2 growth curve (reached-per-depth)"),
    ("explicit 8-hop chain (fallback)",
     "MATCH (a:Atom {uid:'A0'})-[:CONTACT]->(b1:Atom)-[:CONTACT]->(b2:Atom)"
     "-[:CONTACT]->(b3:Atom)-[:CONTACT]->(b4:Atom)-[:CONTACT]->(b5:Atom)"
     "-[:CONTACT]->(b6:Atom)-[:CONTACT]->(b7:Atom)-[:CONTACT]->(b8:Atom) RETURN count(b8)",
     "Q2 fallback if var-len is absent"),
    ("shortestPath",
     "MATCH (a:Atom {uid:'A0'}), (b:Atom {uid:'A5'}) RETURN shortestPath((a)-[:CONTACT*]->(b))",
     "nice-to-have"),

    # --- Q1, the synthon cycle --------------------------------------------
    ("reciprocal cycle pattern",
     "MATCH (a1:Atom)-[h1:CONTACT]->(a2:Atom), (a2)-[h2:CONTACT]->(a1) "
     "WHERE h1.length < 2.9 AND h2.length < 2.9 RETURN a1.uid, a2.uid, h1.length",
     "Q1 acid-dimer ring"),
    ("cycle via two MATCH clauses",
     "MATCH (a1:Atom)-[h1:CONTACT]->(a2:Atom) "
     "MATCH (a2)-[h2:CONTACT]->(a1) RETURN a1.uid, a2.uid",
     "Q1 alternative phrasing"),

    # --- Q3, coformer discovery -------------------------------------------
    ("EXISTS { } subquery",
     "MATCH (c:Component) WHERE EXISTS { MATCH (c)<-[:CONTAINS_COMPONENT]-(:Structure) } "
     "RETURN c.name",
     "Q3 'not yet co-crystallised with' clause"),
    ("NOT EXISTS { } subquery",
     "MATCH (c:Component) WHERE NOT EXISTS { MATCH (c)-[:HAS_FRAGMENT]->(:Fragment) } "
     "RETURN c.name",
     "Q3 negation"),
    ("OPTIONAL MATCH",
     "MATCH (s:Structure) OPTIONAL MATCH (s)-[:SUPERSEDES]->(o:Structure) "
     "RETURN s.cod_id, o.cod_id",
     "Q3 fallback for NOT EXISTS; audit view"),
    ("5-hop similarity chain",
     "MATCH (api:Component)-[:HAS_FRAGMENT]->(f:Fragment)<-[:HAS_FRAGMENT]-(sim:Component)"
     "<-[:CONTAINS_COMPONENT]-(st:Structure)-[:CONTAINS_COMPONENT]->(cand:Component) "
     "RETURN cand.name, count(cand)",
     "Q3 core traversal"),

    # --- Q4 + reporting ----------------------------------------------------
    ("aggregate + non-aggregate",
     "MATCH (f:Fragment)<-[:HAS_FRAGMENT]-(c:Component) RETURN f.fragment_type, count(c)",
     "Q4 motif-frequency table"),
    ("ORDER BY aggregate",
     "MATCH (f:Fragment)<-[:HAS_FRAGMENT]-(c:Component) RETURN f.fragment_type, count(c) AS n ORDER BY n DESC",
     "Q4 ranking"),
    ("WITH",
     "MATCH (a:Atom) WITH count(a) AS n RETURN n",
     "query composition"),
    ("collect()",
     "MATCH (a:Atom) RETURN collect(a.uid)",
     "packing-network assembly for the UI"),
    ("DISTINCT",
     "MATCH (s:Structure) RETURN DISTINCT s.hm_symbol",
     "dedup"),
    ("count(DISTINCT x)",
     "MATCH (s:Structure) RETURN count(DISTINCT s.hm_symbol)",
     "Q3 support counting"),
    ("UNWIND",
     "UNWIND [1,2,3] AS x RETURN x",
     "seed injection"),
    ("IN list literal",
     "MATCH (f:Fragment) WHERE f.fragment_type IN ['carboxylic_acid','hydroxyl'] RETURN f.fragment_type",
     "seed injection"),

    # --- general ergonomics -----------------------------------------------
    ("two sequential MATCH clauses",
     "MATCH (s:Structure)-[:HAS_SITE]->(a:Atom) MATCH (a)-[e:CONTACT]->(b:Atom) "
     "RETURN s.cod_id, count(e)",
     "query readability"),
    ("IS NOT NULL",
     "MATCH (s:Structure) WHERE s.temperature IS NOT NULL RETURN s.cod_id",
     "COD has many missing fields"),
    ("min/max",
     "MATCH ()-[e:CONTACT]->() RETURN min(e.length), max(e.length)",
     "reporting"),
    ("avg",
     "MATCH ()-[e:CONTACT]->() RETURN avg(e.length)",
     "reporting"),
    ("rel type alternation",
     "MATCH (a)-[e:CONTACT|BONDED_TO]->(b) RETURN count(e)",
     "convenience"),
    ("STARTS WITH",
     "MATCH (s:Structure) WHERE s.formula STARTS WITH 'C8' RETURN s.cod_id",
     "formula search in Explore screen"),
    ("CONTAINS",
     "MATCH (s:Structure) WHERE s.formula CONTAINS 'O4' RETURN s.cod_id",
     "formula search in Explore screen"),
    ("CASE",
     "MATCH (s:Structure) RETURN CASE WHEN s.r_factor < 0.04 THEN 'good' ELSE 'ok' END",
     "convenience"),
    ("id()",
     "MATCH (a:Atom) RETURN id(a) LIMIT 2",
     "internal id access"),
    ("labels()",
     "MATCH (n) RETURN labels(n) LIMIT 3",
     "UI node typing"),
    ("keys()",
     "MATCH (s:Structure) RETURN keys(s)",
     "UI property panel"),
    ("parameters $x",
     "MATCH (a:Atom {uid:$seed}) RETURN a.uid",
     "safe seed injection"),
]


def build(client: TuringDB) -> None:
    try:
        client.create_graph(GRAPH)
    except TuringDBException:
        pass
    try:
        client.query(f"LOAD GRAPH {GRAPH}")
    except Exception:  # noqa: BLE001
        pass
    client.set_graph(GRAPH)

    try:
        client.new_change()
        client.query("MATCH (n) DETACH DELETE n")
        client.query("CHANGE SUBMIT")
        client.checkout()
    except Exception:  # noqa: BLE001
        pass

    nodes = [
        f"CREATE (:Atom {{uid:'A{i}', label:'O{i}', element:'O', occupancy:1.0}})"
        for i in range(N_CHAIN)
    ] + [
        "CREATE (:Structure {cod_id:1000001, r_factor:0.041, temperature:293.0, "
        "formula:'C8 H6 O4', hm_symbol:'P 21/c'})",
        "CREATE (:Structure {cod_id:1000002, r_factor:0.038, temperature:100.0, "
        "formula:'C8 H6 O4', hm_symbol:'P 21/c'})",
        "CREATE (:Component {inchikey:'KDY', formula:'C8 H6 O4', is_solvent:false, "
        "name:'terephthalic acid'})",
        "CREATE (:Component {inchikey:'XLY', formula:'H2 O', is_solvent:true, name:'water'})",
        "CREATE (:Fragment {fragment_type:'carboxylic_acid', smarts:'[CX3](=O)[OX2H1]'})",
        "CREATE (:Fragment {fragment_type:'aromatic_ring', smarts:'c1ccccc1'})",
    ]
    client.new_change()
    client.query("\n".join(nodes))
    client.query("COMMIT")  # a MATCH cannot see CREATEs from the same change

    edges = []
    for i in range(N_CHAIN - 1):
        edges.append(
            f"MATCH (x:Atom {{uid:'A{i}'}}), (y:Atom {{uid:'A{i + 1}'}}) "
            f"CREATE (x)-[:CONTACT {{kind:'hbond', length:{2.0 + 0.01 * i:.3f}, "
            f"angle:165.0, symop:'x,y,z', h_inferred:false}}]->(y)"
        )
    edges += [
        "MATCH (x:Atom {uid:'A1'}), (y:Atom {uid:'A0'}) "
        "CREATE (x)-[:CONTACT {kind:'hbond', length:2.05, angle:170.0, "
        "symop:'-x,-y,-z', h_inferred:false}]->(y)",
        "MATCH (x:Atom {uid:'A0'}), (y:Atom {uid:'A1'}) "
        "CREATE (x)-[:BONDED_TO {length:1.43, bond_order:1}]->(y)",
        "MATCH (s:Structure {cod_id:1000001}), (c:Component {is_solvent:false}) "
        "CREATE (s)-[:CONTAINS_COMPONENT {n_copies:2, role:'principal'}]->(c)",
        "MATCH (s:Structure {cod_id:1000001}), (c:Component {is_solvent:true}) "
        "CREATE (s)-[:CONTAINS_COMPONENT {n_copies:1, role:'solvent'}]->(c)",
        "MATCH (s:Structure {cod_id:1000002}), (c:Component {is_solvent:false}) "
        "CREATE (s)-[:CONTAINS_COMPONENT {n_copies:1, role:'principal'}]->(c)",
        "MATCH (c:Component {is_solvent:false}), (f:Fragment {fragment_type:'carboxylic_acid'}) "
        "CREATE (c)-[:HAS_FRAGMENT {atom_uids:'A0,A1'}]->(f)",
        "MATCH (c:Component {is_solvent:true}), (f:Fragment {fragment_type:'aromatic_ring'}) "
        "CREATE (c)-[:HAS_FRAGMENT {atom_uids:'A4,A5'}]->(f)",
        "MATCH (s:Structure {cod_id:1000001}), (a:Atom {uid:'A0'}) CREATE (s)-[:HAS_SITE]->(a)",
        "MATCH (s:Structure {cod_id:1000001}), (a:Atom {uid:'A1'}) CREATE (s)-[:HAS_SITE]->(a)",
        "MATCH (x:Structure {cod_id:1000002}), (y:Structure {cod_id:1000001}) "
        "CREATE (x)-[:SUPERSEDES {reason:'redetermined at 100K', inferred:true}]->(y)",
    ]
    for stmt in edges:
        client.query(stmt)
    client.query("CHANGE SUBMIT")
    client.checkout()


def main() -> int:
    print(f"connecting to {HOST}")
    client = TuringDB(host=HOST)
    print("building fixture ...")
    build(client)
    n = client.query("MATCH (n) RETURN count(n)").iloc[0, 0]
    e = client.query("MATCH ()-[r]->() RETURN count(r)").iloc[0, 0]
    print(f"fixture: {n} nodes, {e} edges\n")

    width = max(len(name) for name, _, _ in CRITICAL)
    npass = 0
    fails: list[tuple[str, str, str]] = []
    for name, cypher, why in CRITICAL:
        try:
            df = client.query(cypher)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).replace("\n", " ")
            # keep just the engine's reason, drop the echoed query block
            for marker in ("-------* ", ": "):
                if marker in msg:
                    pass
            short = msg[:150]
            print(f"  FAIL  {name:<{width}}  {short}")
            fails.append((name, why, short))
        else:
            npass += 1
            print(f"  PASS  {name:<{width}}  rows={len(df)} cols={list(df.columns)}")

    print(f"\n{npass}/{len(CRITICAL)} critical constructs supported")
    if fails:
        print("\nMISSING (impact):")
        for name, why, msg in fails:
            print(f"  - {name}  ->  {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
