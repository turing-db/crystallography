"""Probe the TuringDB Cypher dialect against a live instance.

Run BEFORE writing any real query code. Builds a tiny graph shaped like the
crystallography schema, then tries every Cypher construct the demo needs and
records PASS/FAIL with the server's own error text.

Usage:
    ./.venv/bin/python tests/probe_dialect.py            # human-readable report
    ./.venv/bin/python tests/probe_dialect.py --json      # machine-readable

Nothing here is load-bearing for the demo; it exists so that dialect gaps show
up on day one rather than after 500 lines of Cypher have been written.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from turingdb import TuringDB, TuringDBException

HOST = os.environ.get("TURING_HOST", "http://localhost:6690")
GRAPH = os.environ.get("PROBE_GRAPH", "probe_dialect")


# --------------------------------------------------------------------------
# result bookkeeping
# --------------------------------------------------------------------------


@dataclass
class Result:
    name: str
    group: str
    cypher: str
    ok: bool
    detail: str = ""
    rows: int | None = None
    columns: list[str] = field(default_factory=list)
    sample: str = ""


RESULTS: list[Result] = []


def _short(exc: BaseException) -> str:
    """Collapse a TuringDB error to a single readable line."""
    text = str(exc).strip().replace("\n", " ")
    return text[:400] if text else exc.__class__.__name__


def probe(name: str, group: str, cypher: str, *, client: TuringDB) -> Result:
    """Run one read query and record whether the dialect accepted it."""
    try:
        df = client.query(cypher)
    except (TuringDBException, Exception) as exc:  # noqa: BLE001 - want everything
        res = Result(name, group, cypher, ok=False, detail=_short(exc))
    else:
        cols = [str(c) for c in getattr(df, "columns", [])]
        sample = ""
        try:
            if len(df) > 0:
                sample = df.head(2).to_string(index=False).replace("\n", " | ")[:220]
        except Exception:  # noqa: BLE001
            sample = "<unprintable>"
        res = Result(name, group, cypher, ok=True, rows=len(df), columns=cols, sample=sample)
    RESULTS.append(res)
    return res


def probe_call(name: str, group: str, fn: Callable[[], Any], *, label: str = "") -> Result:
    """Run one SDK call (not raw Cypher) and record whether it worked."""
    try:
        out = fn()
    except Exception as exc:  # noqa: BLE001
        res = Result(name, group, label or f"<python> {name}", ok=False, detail=_short(exc))
    else:
        text = repr(out)
        if len(text) > 300:
            text = text[:300] + "..."
        res = Result(name, group, label or f"<python> {name}", ok=True, sample=text)
    RESULTS.append(res)
    return res


# --------------------------------------------------------------------------
# fixture graph
# --------------------------------------------------------------------------

# A chain long enough to exercise 8-hop traversal, plus a two-way cycle that
# mimics the carboxylic-acid dimer of Q1, plus one structure/component/fragment
# spine so the join-heavy queries have something to bind to.
#
# Property names are chosen to match the real schema EXACTLY, because names
# like `length`, `order`, `angle`, `count` and `position` collide with Cypher
# keywords/functions in some dialects and we need to find that out now.

N_CHAIN = 12


def build_fixture(client: TuringDB) -> None:
    """(Re)build the probe graph from scratch. Idempotent."""
    try:
        client.create_graph(GRAPH)
        print(f"  created graph {GRAPH}")
    except TuringDBException:
        print(f"  graph {GRAPH} already exists")
    try:
        client.load_graph(GRAPH)
    except TuringDBException:
        pass
    try:
        client.query(f"LOAD GRAPH {GRAPH}")
    except Exception:  # noqa: BLE001
        pass
    client.set_graph(GRAPH)

    # wipe
    try:
        client.new_change()
        client.query("MATCH (n) DETACH DELETE n")
        client.query("CHANGE SUBMIT")
        client.checkout()
    except Exception as exc:  # noqa: BLE001
        print(f"  wipe skipped: {_short(exc)}")

    # --- nodes -------------------------------------------------------------
    atom_stmts = []
    for i in range(N_CHAIN):
        atom_stmts.append(
            f"CREATE (:Atom {{uid:'A{i}', label:'O{i}', element:'O', "
            f"fract_x:{0.1 * i:.3f}, fract_y:0.2, fract_z:0.3, "
            f"occupancy:1.0, u_iso:0.05}})"
        )
    node_cypher = "\n".join(
        atom_stmts
        + [
            "CREATE (:Structure {cod_id:1000001, a:5.1, b:6.2, c:7.3, alpha:90.0, "
            "beta:99.5, gamma:90.0, volume:230.4, Z:4, Z_prime:1.0, r_factor:0.041, "
            "temperature:293.0, pressure:101.325, formula:'C8 H6 O4', "
            "hm_symbol:'P 21/c', revision_kind:'redetermination'})",
            "CREATE (:Structure {cod_id:1000002, a:5.2, b:6.3, c:7.4, alpha:90.0, "
            "beta:99.9, gamma:90.0, volume:231.9, Z:4, Z_prime:1.0, r_factor:0.038, "
            "temperature:100.0, pressure:101.325, formula:'C8 H6 O4', "
            "hm_symbol:'P 21/c', revision_kind:'correction'})",
            "CREATE (:SpaceGroup {hm_symbol:'P 21/c', number:14, crystal_system:'monoclinic'})",
            "CREATE (:Element {symbol:'O', atomic_number:8})",
            "CREATE (:Component {inchikey:'KDYFGRWQOYBRFD-UHFFFAOYSA-N', formula:'C8 H6 O4', "
            "charge:0, is_solvent:false, name:'terephthalic acid'})",
            "CREATE (:Component {inchikey:'XLYOFNOQVPJJNP-UHFFFAOYSA-N', formula:'H2 O', "
            "charge:0, is_solvent:true, name:'water'})",
            "CREATE (:Fragment {type:'carboxylic_acid', smarts:'[CX3](=O)[OX2H1]'})",
            "CREATE (:Fragment {type:'aromatic_ring', smarts:'c1ccccc1'})",
            # NB: `volume` is deliberately NOT reused here. TuringDB types
            # properties GLOBALLY, not per-label, so Publication.volume (String)
            # collides with Structure.volume (Double, the cell volume) and the
            # whole CREATE is rejected. Hence `journal_volume`.
            "CREATE (:Publication {doi:'10.1039/x', year:2015, title:'A paper', "
            "journal_volume:'17', pages:'1-9'})",
            "CREATE (:Author {name:'A. Crystallographer', name_key:'crystallographer_a'})",
            "CREATE (:Journal {name:'CrystEngComm', issn:'1466-8033'})",
            "CREATE (:Deposition {deposition_id:'D1', deposited_date:'2015-03-04', "
            "depositor:'dep'})",
        ]
    )
    client.new_change()
    client.query(node_cypher)
    # A MATCH inside a change cannot see nodes CREATEd in the same change, so
    # the node writes must be committed before the edge writes can bind them.
    client.query("COMMIT")

    # --- edges -------------------------------------------------------------
    edge_stmts = []
    # hbond chain A0 -> A1 -> ... for deep traversal
    for i in range(N_CHAIN - 1):
        edge_stmts.append(
            f"MATCH (x:Atom {{uid:'A{i}'}}), (y:Atom {{uid:'A{i + 1}'}}) "
            f"CREATE (x)-[:CONTACT {{kind:'hbond', length:{2.0 + 0.01 * i:.3f}, "
            f"angle:165.0, symop:'x,y,z', h_inferred:false}}]->(y)"
        )
    # reciprocal pair => the Q1 acid-dimer cycle
    edge_stmts.append(
        "MATCH (x:Atom {uid:'A0'}), (y:Atom {uid:'A1'}) "
        "CREATE (y)-[:CONTACT {kind:'hbond', length:2.05, angle:170.0, "
        "symop:'-x,-y,-z', h_inferred:false}]->(x)"
    )
    edge_stmts.append(
        "MATCH (x:Atom {uid:'A2'}), (y:Atom {uid:'A3'}) "
        "CREATE (x)-[:CONTACT {kind:'halogen', length:3.30, angle:158.0, "
        "symop:'x,1/2-y,z', h_inferred:false}]->(y)"
    )
    edge_stmts.append(
        "MATCH (x:Atom {uid:'A4'}), (y:Atom {uid:'A5'}) "
        "CREATE (x)-[:CONTACT {kind:'pi_stack', length:3.62, angle:8.0, "
        "symop:'x,y,1+z', h_inferred:true}]->(y)"
    )
    edge_stmts.append(
        "MATCH (x:Atom {uid:'A0'}), (y:Atom {uid:'A1'}) "
        "CREATE (x)-[:BONDED_TO {length:1.43, order:1}]->(y)"
    )
    edge_stmts += [
        "MATCH (s:Structure {cod_id:1000001}), (g:SpaceGroup {number:14}) "
        "CREATE (s)-[:IN_SPACE_GROUP]->(g)",
        "MATCH (s:Structure {cod_id:1000001}), (e:Element {symbol:'O'}) "
        "CREATE (s)-[:CONTAINS_ELEMENT]->(e)",
        "MATCH (s:Structure {cod_id:1000001}), (c:Component {is_solvent:false}) "
        "CREATE (s)-[:CONTAINS_COMPONENT {count:2, role:'principal'}]->(c)",
        "MATCH (s:Structure {cod_id:1000001}), (c:Component {is_solvent:true}) "
        "CREATE (s)-[:CONTAINS_COMPONENT {count:1, role:'solvent'}]->(c)",
        "MATCH (s:Structure {cod_id:1000002}), (c:Component {is_solvent:false}) "
        "CREATE (s)-[:CONTAINS_COMPONENT {count:1, role:'principal'}]->(c)",
        "MATCH (c:Component {is_solvent:false}), (f:Fragment {type:'carboxylic_acid'}) "
        "CREATE (c)-[:HAS_FRAGMENT {atom_uids:'A0,A1'}]->(f)",
        "MATCH (c:Component {is_solvent:false}), (f:Fragment {type:'aromatic_ring'}) "
        "CREATE (c)-[:HAS_FRAGMENT {atom_uids:'A4,A5'}]->(f)",
        "MATCH (f:Fragment {type:'carboxylic_acid'}), (a:Atom {uid:'A0'}) "
        "CREATE (f)-[:INCLUDES]->(a)",
        "MATCH (f:Fragment {type:'carboxylic_acid'}), (a:Atom {uid:'A1'}) "
        "CREATE (f)-[:INCLUDES]->(a)",
        "MATCH (s:Structure {cod_id:1000001}), (a:Atom {uid:'A0'}) "
        "CREATE (s)-[:HAS_SITE]->(a)",
        "MATCH (s:Structure {cod_id:1000001}), (a:Atom {uid:'A1'}) "
        "CREATE (s)-[:HAS_SITE]->(a)",
        "MATCH (s:Structure {cod_id:1000001}), (p:Publication {year:2015}) "
        "CREATE (s)-[:PUBLISHED_IN]->(p)",
        "MATCH (p:Publication {year:2015}), (j:Journal {issn:'1466-8033'}) "
        "CREATE (p)-[:IN_JOURNAL]->(j)",
        "MATCH (p:Publication {year:2015}), (au:Author {name_key:'crystallographer_a'}) "
        "CREATE (p)-[:AUTHORED_BY {position:1}]->(au)",
        "MATCH (s:Structure {cod_id:1000001}), (d:Deposition {deposition_id:'D1'}) "
        "CREATE (s)-[:FROM_DEPOSITION]->(d)",
        "MATCH (a:Structure {cod_id:1000002}), (b:Structure {cod_id:1000001}) "
        "CREATE (a)-[:SUPERSEDES {reason:'redetermined at 100K', inferred:true}]->(b)",
    ]
    for stmt in edge_stmts:
        client.query(stmt)
    client.query("CHANGE SUBMIT")
    client.checkout()

    n = client.query("MATCH (n) RETURN count(n)")
    print(f"  fixture built: {n.iloc[0, 0]} nodes")


# --------------------------------------------------------------------------
# the probes
# --------------------------------------------------------------------------


def run_probes(client: TuringDB) -> None:
    p = lambda name, group, cypher: probe(name, group, cypher, client=client)  # noqa: E731

    # ---- basics ----------------------------------------------------------
    g = "basics"
    p("MATCH label + props", g, "MATCH (s:Structure) RETURN s.cod_id, s.r_factor")
    p("inline node prop filter", g, "MATCH (s:Structure {cod_id:1000001}) RETURN s.formula")
    p("WHERE on node prop", g, "MATCH (s:Structure) WHERE s.r_factor < 0.04 RETURN s.cod_id")
    p("WHERE label predicate", g, "MATCH (n) WHERE n:Atom RETURN count(n)")
    p("undirected match", g, "MATCH (a:Atom)-[e:CONTACT]-(b:Atom) RETURN count(e)")
    p("return node var", g, "MATCH (s:Structure) RETURN s")
    p("return edge var", g, "MATCH (a)-[e:CONTACT]->(b) RETURN a, e, b")
    p("IS NOT NULL", g, "MATCH (s:Structure) WHERE s.temperature IS NOT NULL RETURN s.cod_id")
    p("boolean prop filter", g, "MATCH (c:Component) WHERE c.is_solvent = false RETURN c.name")
    p("OR chain", g, "MATCH (s:Structure) WHERE s.cod_id = 1000001 OR s.cod_id = 1000002 RETURN s.cod_id")

    # ---- property names that may collide with keywords -------------------
    g = "reserved-property-names"
    p("rel prop .length", g, "MATCH ()-[e:CONTACT]->() RETURN e.length")
    p("rel prop .order", g, "MATCH ()-[e:BONDED_TO]->() RETURN e.order")
    p("rel prop .angle", g, "MATCH ()-[e:CONTACT]->() RETURN e.angle")
    p("rel prop .count", g, "MATCH ()-[e:CONTAINS_COMPONENT]->() RETURN e.count")
    p("rel prop .position", g, "MATCH ()-[e:AUTHORED_BY]->() RETURN e.position")
    p("rel prop .role", g, "MATCH ()-[e:CONTAINS_COMPONENT]->() RETURN e.role")
    p("rel prop .symop", g, "MATCH ()-[e:CONTACT]->() RETURN e.symop")
    p("node prop .number", g, "MATCH (g:SpaceGroup) RETURN g.number")
    p("node prop .volume", g, "MATCH (s:Structure) RETURN s.volume")
    p("node prop .charge", g, "MATCH (c:Component) RETURN c.charge")
    p("node prop .type", g, "MATCH (f:Fragment) RETURN f.type")
    p("node prop .name", g, "MATCH (j:Journal) RETURN j.name")
    p("node prop .year", g, "MATCH (pb:Publication) RETURN pb.year")
    p("node prop .pressure", g, "MATCH (s:Structure) RETURN s.pressure")
    p("node prop .Z / .Z_prime", g, "MATCH (s:Structure) RETURN s.Z, s.Z_prime")
    p("node prop .a/.b/.c cell", g, "MATCH (s:Structure) RETURN s.a, s.b, s.c")
    p("node prop .alpha/.beta/.gamma", g, "MATCH (s:Structure) RETURN s.alpha, s.beta, s.gamma")
    p("node prop .element", g, "MATCH (a:Atom) RETURN a.element")
    p("node prop .occupancy", g, "MATCH (a:Atom) RETURN a.occupancy")

    # ---- relationship predicates ----------------------------------------
    g = "rel-predicates"
    p("inline rel prop filter", g, "MATCH (a)-[e:CONTACT {kind:'hbond'}]->(b) RETURN count(e)")
    p("WHERE on rel prop", g, "MATCH (a)-[e:CONTACT]->(b) WHERE e.kind = 'hbond' RETURN count(e)")
    p("WHERE rel numeric", g, "MATCH (a)-[e:CONTACT]->(b) WHERE e.length < 2.05 RETURN e.length")
    p("WHERE edge label", g, "MATCH (a)-[e]->(b) WHERE e:CONTACT RETURN count(e)")
    p("rel type alternation", g, "MATCH (a)-[e:CONTACT|BONDED_TO]->(b) RETURN count(e)")
    p("two inline rel props", g, "MATCH (a)-[e:CONTACT {kind:'hbond', h_inferred:false}]->(b) RETURN count(e)")

    # ---- variable length -------------------------------------------------
    g = "variable-length"
    p("var-len *1..3", g, "MATCH (a:Atom {uid:'A0'})-[:CONTACT*1..3]->(b:Atom) RETURN count(b)")
    p("var-len *1..8", g, "MATCH (a:Atom {uid:'A0'})-[:CONTACT*1..8]->(b:Atom) RETURN count(b)")
    p("var-len + rel prop inline", g,
      "MATCH (a:Atom {uid:'A0'})-[:CONTACT*1..8 {kind:'hbond'}]->(b:Atom) RETURN count(b)")
    p("var-len untyped", g, "MATCH (a:Atom {uid:'A0'})-[*1..2]->(b) RETURN count(b)")
    p("var-len exact *3", g, "MATCH (a:Atom {uid:'A0'})-[:CONTACT*3]->(b:Atom) RETURN count(b)")
    p("var-len *..8 open lower", g, "MATCH (a:Atom {uid:'A0'})-[:CONTACT*..8]->(b:Atom) RETURN count(b)")
    p("named path + length()", g,
      "MATCH p = (a:Atom {uid:'A0'})-[:CONTACT*1..4]->(b:Atom) RETURN length(p), count(b)")
    p("named path return p", g, "MATCH p = (a:Atom {uid:'A0'})-[:CONTACT*1..2]->(b:Atom) RETURN p")
    p("var-len WHERE on rel var", g,
      "MATCH (a:Atom {uid:'A0'})-[e:CONTACT*1..4]->(b:Atom) WHERE e.kind = 'hbond' RETURN count(b)")
    p("group-by depth", g,
      "MATCH p = (a:Atom {uid:'A0'})-[:CONTACT*1..8]->(b:Atom) RETURN length(p), count(b)")
    p("shortestPath", g,
      "MATCH (a:Atom {uid:'A0'}), (b:Atom {uid:'A5'}) RETURN shortestPath((a)-[:CONTACT*]->(b))")

    # ---- multi-pattern / multi-clause ------------------------------------
    g = "multi-pattern"
    p("comma-joined patterns", g,
      "MATCH (f:Fragment {type:'carboxylic_acid'})<-[:HAS_FRAGMENT]-(c:Component), "
      "(c)<-[:CONTAINS_COMPONENT]-(s:Structure) RETURN s.cod_id, f.type")
    p("two sequential MATCH clauses", g,
      "MATCH (s:Structure)-[:HAS_SITE]->(a:Atom) "
      "MATCH (a)-[e:CONTACT]->(b:Atom) RETURN s.cod_id, count(e)")
    p("three sequential MATCH clauses", g,
      "MATCH (s:Structure)-[:CONTAINS_COMPONENT]->(c:Component) "
      "MATCH (c)-[:HAS_FRAGMENT]->(f:Fragment) "
      "MATCH (s)-[:HAS_SITE]->(a:Atom) RETURN s.cod_id, f.type, count(a)")
    p("4-hop chain one pattern", g,
      "MATCH (s:Structure)-[:CONTAINS_COMPONENT]->(c:Component)-[:HAS_FRAGMENT]->"
      "(f:Fragment)<-[:HAS_FRAGMENT]-(c2:Component)<-[:CONTAINS_COMPONENT]-(s2:Structure) "
      "RETURN s.cod_id, s2.cod_id, f.type")
    p("cycle pattern (Q1 shape)", g,
      "MATCH (a1:Atom)-[h1:CONTACT]->(a2:Atom), (a2)-[h2:CONTACT]->(a1) "
      "WHERE h1.length < 2.9 AND h2.length < 2.9 RETURN a1.uid, a2.uid, h1.length, h2.length")
    p("self-join with inequality", g,
      "MATCH (c1:Component)-[:HAS_FRAGMENT]->(f:Fragment)<-[:HAS_FRAGMENT]-(c2:Component) "
      "WHERE c1.inchikey <> c2.inchikey RETURN c1.name, c2.name")

    # ---- aggregation / projection ---------------------------------------
    g = "aggregation"
    p("count(*)", g, "MATCH (a:Atom) RETURN count(*)")
    p("count(n)", g, "MATCH (a:Atom) RETURN count(a)")
    p("aggregate + non-aggregate", g,
      "MATCH (f:Fragment)<-[:HAS_FRAGMENT]-(c:Component) RETURN f.type, count(c)")
    p("count with alias", g, "MATCH (a:Atom) RETURN count(a) AS n")
    p("collect()", g, "MATCH (a:Atom) RETURN collect(a.uid)")
    p("DISTINCT", g, "MATCH (s:Structure) RETURN DISTINCT s.hm_symbol")
    p("count(DISTINCT x)", g, "MATCH (s:Structure) RETURN count(DISTINCT s.hm_symbol)")
    p("min/max/avg/sum", g, "MATCH (a)-[e:CONTACT]->(b) RETURN min(e.length), max(e.length)")
    p("avg", g, "MATCH (a)-[e:CONTACT]->(b) RETURN avg(e.length)")
    p("sum", g, "MATCH (a)-[e:CONTACT]->(b) RETURN sum(e.length)")
    p("ORDER BY property", g, "MATCH (a)-[e:CONTACT]->(b) RETURN e.length ORDER BY e.length")
    p("ORDER BY alias", g, "MATCH (a:Atom) RETURN count(a) AS n ORDER BY n")
    p("ORDER BY agg no alias", g,
      "MATCH (f:Fragment)<-[:HAS_FRAGMENT]-(c:Component) RETURN f.type, count(c) ORDER BY count(c) DESC")
    p("LIMIT", g, "MATCH (a:Atom) RETURN a.uid LIMIT 3")
    p("SKIP + LIMIT", g, "MATCH (a:Atom) RETURN a.uid ORDER BY a.uid SKIP 2 LIMIT 3")

    # ---- constructs the spec flagged as uncertain ------------------------
    g = "uncertain-constructs"
    p("OPTIONAL MATCH", g,
      "MATCH (s:Structure) OPTIONAL MATCH (s)-[:SUPERSEDES]->(old:Structure) "
      "RETURN s.cod_id, old.cod_id")
    p("EXISTS { } subquery", g,
      "MATCH (c:Component) WHERE EXISTS { MATCH (c)<-[:CONTAINS_COMPONENT]-(:Structure) } "
      "RETURN c.name")
    p("NOT EXISTS { } subquery", g,
      "MATCH (c:Component) WHERE NOT EXISTS { MATCH (c)-[:HAS_FRAGMENT]->(:Fragment) } "
      "RETURN c.name")
    p("negated path predicate", g,
      "MATCH (c:Component) WHERE NOT (c)-[:HAS_FRAGMENT]->() RETURN c.name")
    p("WITH", g, "MATCH (a:Atom) WITH count(a) AS n RETURN n")
    p("WITH + filter", g,
      "MATCH (f:Fragment)<-[:HAS_FRAGMENT]-(c:Component) WITH f, count(c) AS n "
      "WHERE n > 0 RETURN f.type, n")
    p("UNWIND", g, "UNWIND [1,2,3] AS x RETURN x")
    p("IN list literal", g,
      "MATCH (f:Fragment) WHERE f.type IN ['carboxylic_acid','hydroxyl'] RETURN f.type")
    p("keys(n)", g, "MATCH (s:Structure) RETURN keys(s)")
    p("labels(n)", g, "MATCH (n) RETURN labels(n) LIMIT 3")
    p("labels() in WHERE", g, "MATCH (n) WHERE labels(n) = 'Atom' RETURN count(n)")
    p("id() function", g, "MATCH (a:Atom) RETURN id(a) LIMIT 2")
    p("CASE expression", g,
      "MATCH (s:Structure) RETURN CASE WHEN s.r_factor < 0.04 THEN 'good' ELSE 'ok' END")
    p("toInteger", g, "MATCH (s:Structure) WHERE s.cod_id > toInteger('1000000') RETURN s.cod_id")
    p("toFloat", g, "MATCH (s:Structure) RETURN s.volume * toFloat('2.0')")
    p("arithmetic in RETURN", g, "MATCH (s:Structure) RETURN s.a * s.b * s.c")
    p("string equality on prop", g, "MATCH (g2:SpaceGroup) WHERE g2.hm_symbol = 'P 21/c' RETURN g2.number")
    p("STARTS WITH", g, "MATCH (s:Structure) WHERE s.formula STARTS WITH 'C8' RETURN s.cod_id")
    p("CONTAINS", g, "MATCH (s:Structure) WHERE s.formula CONTAINS 'O4' RETURN s.cod_id")
    p("parameter $x", g, "MATCH (a:Atom {uid:$seed}) RETURN a.uid")
    p("internal-id seed WHERE a = n", g, "MATCH (a)-->(b) WHERE a = 0 RETURN count(b)")

    # ---- introspection ---------------------------------------------------
    g = "introspection"
    p("CALL db.labels()", g, "CALL db.labels()")
    p("CALL db.edgeTypes()", g, "CALL db.edgeTypes()")
    p("CALL db.propertyTypes()", g, "CALL db.propertyTypes()")
    p("CALL db.history()", g, "CALL db.history()")
    p("CALL db.getNodes", g, "CALL db.getNodes(['Atom'])")
    p("LIST GRAPH", g, "LIST GRAPH")
    p("LIST AVAILABLE GRAPHS", g, "LIST AVAILABLE GRAPHS")


def run_versioning_probes(client: TuringDB) -> None:
    """Probe the git-style versioning surface: commits, branches, tags, checkout."""
    g = "versioning-sdk"

    for meth in [
        "new_change", "commit", "checkout", "history", "set_commit", "set_graph",
        "create_graph", "load_graph", "list_available_graphs", "list_loaded_graphs",
        "create_branch", "branches", "list_branches", "switch_branch", "tag", "tags",
        "create_tag", "list_tags", "diff", "merge", "reset", "status",
    ]:
        has = hasattr(client, meth)
        RESULTS.append(
            Result(f"SDK has .{meth}()", "versioning-sdk-surface", f"hasattr(client, '{meth}')",
                   ok=has, detail="" if has else "absent")
        )

    probe_call("client.history()", g, lambda: client.history())
    probe_call("client.list_available_graphs()", g, lambda: client.list_available_graphs())
    probe_call("client.list_loaded_graphs()", g, lambda: client.list_loaded_graphs())

    g = "versioning-cypher"
    pv = lambda name, cypher: probe(name, g, cypher, client=client)  # noqa: E731
    pv("CALL db.history()", "CALL db.history()")
    pv("LIST BRANCH", "LIST BRANCH")
    pv("LIST BRANCHES", "LIST BRANCHES")
    pv("SHOW BRANCHES", "SHOW BRANCHES")
    pv("LIST TAG", "LIST TAG")
    pv("LIST TAGS", "LIST TAGS")
    pv("CREATE BRANCH probe_branch", "CREATE BRANCH probe_branch")
    pv("BRANCH probe_branch2", "BRANCH probe_branch2")
    pv("CREATE TAG probe_tag", "CREATE TAG probe_tag")
    pv("TAG probe_tag2", "TAG probe_tag2")
    pv("CHECKOUT main", "CHECKOUT main")

    # time travel: grab a real commit hash from history, then try to read at it
    try:
        hist = client.query("CALL db.history()")
        first = str(hist.iloc[0, 0]).replace("(HEAD)", "").strip()
        RESULTS.append(Result("history HEAD hash", "versioning-cypher",
                              "CALL db.history() -> iloc[0,0]", ok=True, sample=first))
        pv(f"LOAD COMMIT '<hash>'", f"LOAD COMMIT '{first}'")
        probe_call("client.set_commit(hash)", "versioning-sdk",
                   lambda: client.set_commit(first), label=f"client.set_commit('{first}')")
        probe_call("client.checkout() back to HEAD", "versioning-sdk", lambda: client.checkout())
    except Exception as exc:  # noqa: BLE001
        RESULTS.append(Result("history HEAD hash", "versioning-cypher",
                              "CALL db.history()", ok=False, detail=_short(exc)))


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def report() -> None:
    groups: dict[str, list[Result]] = {}
    for r in RESULTS:
        groups.setdefault(r.group, []).append(r)

    n_ok = sum(1 for r in RESULTS if r.ok)
    print("\n" + "=" * 78)
    print(f"TuringDB dialect probe — {n_ok}/{len(RESULTS)} constructs accepted")
    print(f"host={HOST} graph={GRAPH}")
    print("=" * 78)

    for name, rs in groups.items():
        ok = sum(1 for r in rs if r.ok)
        print(f"\n### {name}  ({ok}/{len(rs)})")
        for r in rs:
            mark = "PASS" if r.ok else "FAIL"
            extra = ""
            if r.ok and r.rows is not None:
                extra = f" rows={r.rows}"
                if r.columns:
                    extra += f" cols={r.columns}"
            print(f"  [{mark}] {r.name}{extra}")
            if not r.ok:
                print(f"         cypher: {r.cypher[:150]}")
                print(f"         error : {r.detail}")
            elif r.sample:
                print(f"         -> {r.sample[:180]}")

    print("\n" + "-" * 78)
    print("FAILURES (each of these forces a query rewrite):")
    for r in RESULTS:
        if not r.ok:
            print(f"  - [{r.group}] {r.name}: {r.detail[:160]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    ap.add_argument("--no-build", action="store_true", help="skip fixture rebuild")
    args = ap.parse_args()

    print(f"connecting to {HOST} ...")
    client = TuringDB(host=HOST)

    if not args.no_build:
        print("building fixture graph ...")
        build_fixture(client)
    else:
        try:
            client.load_graph(GRAPH)
        except Exception:  # noqa: BLE001
            pass
        client.set_graph(GRAPH)

    print("running probes ...")
    run_probes(client)
    try:
        run_versioning_probes(client)
    except Exception:  # noqa: BLE001
        traceback.print_exc()

    if args.json:
        print(json.dumps([r.__dict__ for r in RESULTS], indent=2))
    else:
        report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
