# TuringDB Cypher dialect — verified surface

Every line below was tested against a running instance, not read from docs.
Reproduce with:

```bash
TURING_HOST=http://localhost:6691 ./.venv/bin/python tests/probe_turingdb_syntax.py
TURING_HOST=http://localhost:6691 ./.venv/bin/python tests/probe_followup.py
```

- **Server**: TuringDB `v1.37` (commit `228a7125`, built 2026-08-21), the binary
  shipped inside the `turingdb==1.37` PyPI wheel.
- **SDK**: `turingdb` 1.37.
- Tested 2026-09-03.

A second, older source build (`3c113474b`, 2026-07-16) is also on the box and
behaves the same on every construct below except that it additionally **crashes
the server** on `CALL db.getNodes([...])`. We do not use it.

---

## 1. The four findings that shape the whole project

### 1.1 Variable-length paths work, but NOT with an edge-type filter

TuringDB does **not** use Neo4j's `-[:R*1..8]->`. It uses **postfix quantifiers**:

```cypher
MATCH (a)-[e]->+(b)      -- one or more hops
MATCH (a)-[e]->*(b)      -- zero or more hops
MATCH (a)-[e]->{2,4}(b)  -- 2 to 4 hops
MATCH (a)-[e]->{8,8}(b)  -- exactly 8 hops
```

Adding an edge type is rejected by the analyzer:

```
MATCH (a:Atom)-[e:CONTACT]->{1,8}(b:Atom)
  -> ANALYZE_ERROR: Edge type filters are not supported with variable-length paths
```

The same error appears for `-[:CONTACT]->{1,8}`, for an inline property map on a
quantified edge, and for a `WHERE e.kind = ...` predicate over a quantified edge
variable. Undirected (`-[e]-{1,3}`) and reverse (`<-[e]-{1,3}`) quantifiers are
also rejected *when typed*; untyped they are fine.

**Untyped quantified traversal is fully working**, including seeded and to depth 12:

```cypher
MATCH (a:Atom {uid:'A0'})-[e]->{1,8}(b:Atom) RETURN count(b)     -- 30
MATCH (a:Atom {uid:'A0'})-[e]->{8,8}(b:Atom) RETURN count(b)     --  4
MATCH (a:Atom {uid:'A0'})-[e]->{1,12}(b:Atom) RETURN count(b)    -- 44
MATCH (a)-[e]->{1,8}(b:Atom) WHERE a = 0 RETURN count(b)         -- seed by internal id
MATCH (a:Atom {uid:'A0'})-[e]->{1,8}(b:Atom) WHERE b.element='O' RETURN count(b)
```

**Consequence for Q2.** The contact network must be materialised as a graph in
which the traversable edges are the *only* edges between the traversed nodes, so
that an untyped quantifier is exactly a contact traversal. See
`docs/QUESTIONS.md` Q-1.

### 1.2 Property types are GLOBAL, not per-label

One property name has one type across the entire graph. `Structure.volume`
(Double, cell volume) and `Publication.volume` (String, journal volume) cannot
coexist:

```
Property type 'volume' already exists with a different type 'Double' vs. 'String'
```

Schema renames forced by this: `Publication.volume` -> `journal_volume`.

### 1.3 `type` is a reserved token

```
CREATE (:Fragment {type:'carboxylic_acid'})
  -> PARSE_ERROR: syntax error, unexpected TYPE
```

Schema rename forced by this: `Fragment.type` -> `fragment_type`.
Other reserved words found in this codebase's history: `s3`, `graph`, `from`.

### 1.4 `IS NULL` / `IS NOT NULL` do not work

```
MATCH (s:Structure) WHERE s.temperature IS NOT NULL RETURN s.cod_id
  -> ANALYZE_ERROR: Operands are not valid or compatible types: 'Double' and 'Null'
```

This holds even when the property genuinely exists on some nodes and not others.

However, absent properties behave usefully in two ways:
- **Projection is safe**: `RETURN s.cod_id, s.temperature` returns a row for
  every structure, with a null in the missing cells.
- **Comparison silently excludes**: `WHERE s.temperature < 200.0` returns only
  the structures that both have the property and satisfy it.

**Consequence for ingest.** COD is full of missing fields and the spec forbids
fabricating them. We therefore write an explicit boolean companion flag for
every optional numeric field (`has_temperature`, `has_pressure`, `has_r_factor`,
…) so that "which structures lack a measured temperature?" stays answerable.

---

## 2. Supported — verified PASS

| Construct | Example |
|---|---|
| Node label + property match | `MATCH (s:Structure {cod_id:1000001}) RETURN s.formula` |
| `WHERE` on node/edge property | `WHERE e.kind = 'hbond'`, `WHERE e.length < 2.9` |
| `WHERE` on label | `MATCH (n) WHERE n:Atom RETURN count(n)` |
| Seed by internal id | `MATCH (n) WHERE n = 1 RETURN labels(n)` |
| Inline edge property filter | `-[e:CONTACT {kind:'hbond'}]->` (fixed-length only) |
| Multi-pattern comma joins | `MATCH (a)-->(b), (b)-->(c), (c)-->(d)` |
| Self-join with `<>` | `WHERE c1.inchikey <> c2.inchikey` |
| Untyped quantifiers `+ * {m,n}` | see 1.1 |
| `shortestPath` (statement form) | see 3.1 |
| `count()` / `avg()` as a single return item | `RETURN count(*)`, `RETURN avg(e.length)` |
| Aggregates combined in one item | `RETURN count(e) + avg(e.length)` |
| `ORDER BY` projected property, `SKIP`, `LIMIT` | `RETURN e.length ORDER BY e.length LIMIT 5` |
| `AS` aliasing | `RETURN s.cod_id AS cod_id` |
| Arithmetic in `RETURN` | `RETURN s.r_factor * 100.0` |
| `labels(n)`, `edgeType(e)` | `RETURN labels(n), edgeType(e), labels(m)` |
| List literal (literal elements only) | `RETURN [1,2,3] AS nums` |
| `UNWIND` (literal lists only) | `UNWIND [1,2,3] AS x RETURN x` |
| Property indexes | `CREATE INDEX uid_index FOR (n) ON n.uid` |
| `CALL db.labels() / edgeTypes() / propertyTypes() / history() / showIndexes()` | |
| `CALL ... YIELD ... RETURN *` | `CALL db.propertyTypes() YIELD propertyType, valueType RETURN *` |
| `LOAD JSONL '<f>' AS <graph>` | full round-trip verified, see 3.2 |
| Change workflow | `CHANGE NEW` / `COMMIT` / `CHANGE SUBMIT` / `CHANGE LIST` |
| `DETACH DELETE` | works despite being undocumented |

## 3. Notable working forms

### 3.1 shortestPath is a statement, not a pattern function

```cypher
MATCH (n:Atom {uid:'A0'}), (m:Atom {uid:'A5'})
shortestPath(n, m, length, dist, path)
RETURN dist, path
-- dist = 9.53, path = [11, 11, 10, 9, 9, 8, 8, 7, 7, 6, 6]
```

Signature: `shortestPath(sourceSet, targetSet, edgePropName, distanceOut, pathOut)`.
Variables consumed by the processor cannot also appear in `RETURN`.

### 3.2 LOAD JSONL round-trips everything we need

One file, one JSON object per line, Neo4j APOC `apoc.export.json.all` shape.
Node ids and relationship ids must each start at 0 and increment with no gaps.
File must live in `<turing-dir>/data/`.

```jsonl
{"type":"node","id":"0","labels":["Atom"],"properties":{"uid":"J0","element":"O"}}
{"type":"relationship","id":"0","label":"CONTACT","start":{"id":"0"},"end":{"id":"1"},"properties":{"kind":"hbond","length":2.11}}
```

Verified to preserve node labels, edge types, and String/Double/Int typing.
Note the asymmetry: nodes take `labels` (array), relationships take `label`
(scalar), and endpoints are `start`/`end`.

This is the ingest format for the project: the **same file** loads into TuringDB
via `LOAD JSONL` and into Neo4j via `apoc.import.json`, which is exactly what the
benchmark needs ("same JSONL, loaded into both").

---

## 4. NOT supported — verified FAIL

Each of these was tried and rejected by the running engine.

| Construct | Engine response |
|---|---|
| Edge type + variable length | `Edge type filters are not supported with variable-length paths` |
| `IS NULL` / `IS NOT NULL` | `Operands are not valid or compatible types: 'Double' and 'Null'` |
| Reciprocal cycle `(a)-->(b), (b)-->(a)` | `PLAN_ERROR: Loop detected. This is not supported yet` |
| Aggregate + non-aggregate in one RETURN | `ANALYZE_ERROR` — aggregates need a single return item |
| `min()` / `max()` / `sum()` | `Function 'min' does not exist` (only `count`, `avg`) |
| `ORDER BY <alias>` | `Variable n not found` |
| `WITH` | `Not implemented: WITH` |
| `collect()` | `Not implemented: COLLECT` |
| `DISTINCT`, `count(DISTINCT x)` | `DISTINCT is not supported` |
| `OPTIONAL MATCH` | `ANALYZE_ERROR` |
| `EXISTS { }`, `NOT EXISTS { }` | `PARSE_ERROR` |
| Negated pattern `WHERE NOT (a)-->()` | `ANALYZE_ERROR` |
| Sequential `MATCH ... MATCH ...` | `ANALYZE_ERROR` (use comma-joined patterns) |
| `IN` + list | `Binary operator IN not yet supported` |
| `STARTS WITH`, `CONTAINS` | `String expressions are currently not supported` |
| `CASE ... WHEN` | `PARSE_ERROR` |
| `keys(n)` | `Function 'keys' is not supported in expressions` |
| `id(n)` | `Function 'id' does not exist` (use `WHERE n = <int>`) |
| `labels(n)` inside `WHERE` | `Unsupported binary operation` (RETURN only) |
| Query parameters `$x` | `Not implemented: Parameters` |
| Edge type alternation `-[:A\|B]->` | `Not implemented` |
| `CALL db.procedures()` | `syntax error, unexpected PROCEDURES` |
| Branches beyond `main`; tags | no statement exists — see QUESTIONS.md Q-3 |

---

## 5. Rewrites this forces on the four demo queries

| Query | Spec form | What we must write |
|---|---|---|
| Q1 synthon | `(a1)-[h1]->(a2), (a2)-[h2]->(a1)` | Loop rejected. Join two independent edge patterns on properties: `MATCH (a1)-[h1:CONTACT]->(a2), (a3)-[h2:CONTACT]->(a4) WHERE a1.uid = a4.uid AND a2.uid = a3.uid` — verified to return the correct reciprocal pairs. |
| Q2 depth | `-[:CONTACT*1..8 {kind:'hbond'}]->` + `length(p)` | No edge-type filter and no `length(p)`. Run one query per depth with an exact quantifier `{k,k}` against a contact-only graph. Gives the growth curve directly. |
| Q3 coformer | `NOT EXISTS { ... }`, `count(*)`, `ORDER BY support` | No `EXISTS`, no `DISTINCT`, no aggregate-with-grouping. Run the 5-hop join returning candidate rows (verified working), then do the anti-join, dedup, counting and ranking **client-side in Python**. |
| Q4 versioned stats | `RETURN fa.type, count(*) ... ORDER BY n DESC LIMIT 50` | No grouping. Issue one `count()` query per fragment type per commit, then assemble the table client-side. Time travel itself works via `checkout(commit=<hash>)`. |

Every one of these rewrites is recorded here so the walkthrough can state plainly
what was changed and why.
