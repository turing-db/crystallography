# Tutorial

How the graph is built, what is in it, and how to ask it questions — from a
shell or from the browser.

1. [Running it](#1-running-it)
2. [The graph model](#2-the-graph-model)
3. [Querying from the command line](#3-querying-from-the-command-line)
4. [The Cypher dialect: what works and what does not](#4-the-cypher-dialect-what-works-and-what-does-not)
5. [Worked questions](#5-worked-questions)
6. [The browser studio](#6-the-browser-studio)
7. [Time travel](#7-time-travel)
8. [How the data gets in](#8-how-the-data-gets-in)
9. [Writing fast queries](#9-writing-fast-queries)

---

## 1. Running it

```bash
git clone https://github.com/turing-db/crystallography
cd crystallography
./run.sh --ingest        # first run only
```

The structures themselves are not in the repository: they are 2.8 GB of CIFs
belonging to COD, so the first run acquires them. It takes roughly 25 minutes
and needs network access to `crystallography.net`. Afterwards, `./run.sh` on its
own starts the server and the studio on the data already built.

`run.sh` installs dependencies with `uv`, starts a TuringDB server on port 6691
with the data directory `./turing-data`, builds the front end and serves it on
port 8087. The server binary ships inside the `turingdb` Python wheel; there is
no separate database to install.

### Checking it is alive

```bash
crystal graphs     # graphs on the server, and whether each is loaded
crystal facts      # corpus summary, every figure re-derived from the graph
```

If `crystal graphs` shows a graph as **not loaded**, load it before querying —
after a server restart, graphs are on disk but not resident, and an unloaded
graph answers `GRAPH_NOT_FOUND` rather than returning nothing:

```python
from turingdb import TuringDB
c = TuringDB(host="http://localhost:6691")
c.load_graph("cod_slice_v2")     # ~4 s for the full corpus
```

### The four graphs

| graph | what it is |
|---|---|
| `cod_slice_v2` | the full model — this is the one you want |
| `cod_contacts_v2` | atoms + located-H hydrogen bonds only, for variable-length traversal |
| `cod_versioned` | the same corpus committed chronologically, one commit per publication year |
| `cod_versions` | the ledger mapping `cod@<year>` to a commit hash |

---

## 2. The graph model

Nine node labels and twelve edge types, all populated.

```
                    ┌──────────────┐
    IN_SPACE_GROUP  │  SpaceGroup  │  hm_symbol, number, crystal_system,
   ┌───────────────▶│              │  is_centrosymmetric
   │                └──────────────┘
   │                ┌──────────────┐
   │  CONTAINS_     │   Element    │  symbol, atomic_number
   │  ELEMENT  ────▶└──────────────┘
   │
┌──┴────────────┐  HAS_SITE   ┌──────────┐  IN_COMPONENT  ┌─────────────┐
│   Structure   │────────────▶│   Atom   │───────────────▶│  Component  │
│               │             │          │                │             │
│ cod_id        │             │ uid      │                │ inchikey    │
│ hm_symbol     │             │ label    │                │ formula     │
│ a b c α β γ   │             │ element  │                │ charge      │
│ Z, Z_prime    │             │ fract_*  │                │ is_solvent  │
│ r_factor      │             │ occupancy│                │ is_polymeric│
│ temperature   │             └──────────┘                └─────────────┘
│ net_dim       │              │      │                     │
│ net_dim_weak  │       BONDED_TO   CONTACT            HAS_FRAGMENT
│ has_hydrogens │              │      │                     ▼
└───────────────┘              ▼      ▼                ┌─────────────┐
   │        │            (Atom)    (Atom)              │  Fragment   │
   │        │                                          │ fragment_   │
   │        └── CONTAINS_COMPONENT ──▶ (Component)      │   type      │
   │                 role: principal | solvent |        │ smarts      │
   │                       counter_ion                  └─────────────┘
   │
   ├── PUBLISHED_IN ──▶ (Publication) ── IN_JOURNAL ──▶ (Journal)
   │                          │
   │                          └── AUTHORED_BY ──▶ (Author)
   └── SUPERSEDES ────▶ (Structure)
```

### The edge that matters: `CONTACT`

Every intermolecular contact is an edge between two `Atom` nodes, carrying:

| property | meaning |
|---|---|
| `kind` | `hbond` · `hbond_weak` · `halogen` · `close_contact` |
| `h_inferred` | `true` when no hydrogen was refined and a heavy-atom cutoff was used |
| `length` | H···A, or X···A for a halogen bond, or D···A when `h_inferred` |
| `angle` | D–H···A, or C–X···A |
| `symop` | the generating operation in CIF form, e.g. `2_565` |
| `symop_triplet` | the same operation written out, e.g. `-x+1,-y+1,-z+1` |
| `is_involution` | `true` if the operation is its own inverse |
| `cod_id` | the structure the contact belongs to |

**Always filter on `kind` and `h_inferred`.** The three populations are
deliberately separate and blending them silently is how packing statistics go
wrong. Nearly every query in this tutorial begins:

```cypher
WHERE r.kind = 'hbond' AND r.h_inferred = false
```

**`is_involution` separates a ring motif from a chain.** A centrosymmetric dimer
is generated by an inversion, mirror or two-fold — an operation that is its own
inverse, so it relates exactly two molecules. A catemer is generated by a 2₁
screw or a glide, which has infinite order and builds a chain. Both have a
two-fold *rotation* part, so nothing short of composing the operation with
itself distinguishes them. It is precomputed at ingest.

**The identity operation passes this test**, and about 17% of the hits in the
dimer query carry `symop = '1_555'`. Those are contacts between two
crystallographically independent molecules in a Z′ > 1 cell — real pairs, but
generated by no symmetry at all. Add `AND h.symop <> '1_555'` if you want only
the symmetry-generated motif, and be aware that doing so discards genuine
general-position dimers.

### Two things that are easy to get wrong

**`Fragment` is a 16-node lookup table, not a per-molecule annotation.** There
is one node per functional-group type for the whole corpus, and `HAS_FRAGMENT`
says *this molecule contains at least one of these somewhere*. There is **no
atom-level fragment membership**: nothing records which oxygen belongs to the
carboxyl group. A query reading `f.fragment_type = 'carboxylic_acid' AND
a1.element = 'O'` therefore means "some oxygen of a molecule that has a COOH
somewhere", which is not the same thing and will admit alcohols, nitro oxygens
and esters in polyfunctional molecules. Treat the shipped motif queries as
*candidate generators* and confirm the hits.

**`Component` is deduplicated across structures.** One node per distinct
molecular identity for the whole corpus, so `c.formula` is that shared node's
formula and `CONTAINS_COMPONENT` fans out to every structure containing it.
That is what makes cross-structure questions one hop, and it is also why any
pattern reaching two atoms through two Component nodes needs scoping to a
structure.

### Two things about the representation

**The stored graph is a quotient graph.** Nodes are asymmetric-unit sites, and
each contact edge carries the operation to its neighbour. A hydrogen-bonded
chain running through the crystal is therefore a short *cycle*, not a long path.
Counting reachable nodes by depth reports everything as isolated; see
`ingest/netdim.py` for the correct treatment.

**A centrosymmetric dimer is one edge, not two.** Its second hydrogen bond is
the symmetry image of the first, so the quotient graph stores it once. If you
are counting physical hydrogen bonds rather than stored edges, an
involution-generated edge counts twice.

---

## 3. Querying from the command line

```bash
crystal facts                    # corpus summary
crystal list                     # the named queries
crystal run dimer                # run one
crystal run dimer --cypher       # print its Cypher instead of running it
crystal query "MATCH (s:Structure) RETURN count(s)"
crystal at 2009 "MATCH (s:Structure) RETURN count(s)"
crystal commits                  # the version ledger
crystal graphs                   # graphs on the server
```

Output options work before or after the subcommand:

```bash
crystal run dimer --limit 0 --csv > dimers.csv
crystal query "MATCH (s:Structure) RETURN s.cod_id, s.r_factor" --json
crystal --graph cod_contacts_v2 query "MATCH (n:Atom) RETURN count(n)"
```

`TURING_HOST` and `TURING_GRAPH` set the defaults.

The named queries are a starting point, not a fixed menu — `--cypher` prints the
query so you can paste it into `crystal query`, change a predicate and re-run.

### From Python

```python
from turingdb import TuringDB

c = TuringDB(host="http://localhost:6691")
c.load_graph("cod_slice_v2")          # only needed once per server start
c.set_graph("cod_slice_v2")

df = c.query("MATCH (s:Structure) WHERE s.net_dim = 2 RETURN count(s)")
print(df)                              # results come back as a DataFrame
```

### Over REST

Useful from any language. The body is the raw Cypher string, not JSON-wrapped.

```bash
curl -s -X POST "http://localhost:6691/query?graph=cod_slice_v2" \
     -H "Content-Type: application/json" \
     -d "MATCH (s:Structure) RETURN count(s)"
```

The response is **column-oriented and chunked**:

```json
{"header": {"column_names": ["count(s)"], "column_types": ["UInt64"]},
 "data": [[[84801]]], "time": 0.144}
```

`data` is a list of chunks; each chunk is a list of *columns*, not rows. To
rebuild rows from chunk `k`: read `n = chunk[0].length`, then
`row[i][c] = chunk[c][i]`.

**Failures arrive as HTTP 200 with a non-null top-level `error`.** Checking the
status code is not enough.

---

## 4. The Cypher dialect: what works and what does not

TuringDB implements most of Cypher. The gaps that matter in practice:

**Not supported.** `WITH`, `collect()`, `OPTIONAL MATCH`, `UNION`, `DISTINCT`,
`MERGE`, variable-length path aliases, `IN`, `CONTAINS` / `STARTS WITH` / `=~`.

**Only two aggregates exist.** `count()` and `avg()` work; `min()`, `max()`,
`sum()` and `stdev()` are all rejected. So "the shortest contact", "the longest
cell axis" and "the total number of sites" cannot be expressed as aggregates —
project the column and reduce it client-side, which is what `crystal` does.

**No GROUP BY, and aggregates do not combine with other return items.**
`RETURN f.fragment_type, count(c)` is rejected, and so is an aggregate inside
`ORDER BY`. A "how many of each" question is expressed as a flat projection and
counted client-side — which is exactly what `crystal run fragments` does, and
why `--cypher` shows a query with no `count()` in it.

**`IS NULL` does not work.** Every optional numeric field has an explicit
boolean companion instead: `has_temperature`, `has_r_factor`, `has_net_dim`,
`has_inchikey`. Absent properties still project safely as nulls, and comparisons
silently exclude them.

**There are no query parameters.** `$name` is a `PARSE_ERROR: Not implemented:
Parameters`, so a value can only reach a query by string interpolation — which
means **the usual protection against injection is not available**:

```cypher
WHERE g.hm_symbol = 'P -1'          -- 1
WHERE g.hm_symbol = 'P -1' OR 1=1   -- 214
```

Every query in this repository interpolates, because there is no alternative.
That is acceptable here: the corpus is public CC0 data, the server is bound to
localhost, and the demo has no untrusted input. **It is not acceptable in
anything that accepts input from a user**, and the patterns in this repository
should not be lifted into such a service without an escaping or allow-listing
layer in front of them. Until parameters land, treat a Cypher string as
something you build, never something a caller supplies.

**`type` is a reserved word.** The edge-type function is `edgeType(r)`, and a
property named `type` needs backticks. This is why fragments carry
`fragment_type`.

**Property types are global, not per label.** One property name has one type
across the whole graph. `Publication.volume` is `journal_volume` here because
`Structure.cell_volume` already claimed a numeric `volume`.

**Projecting a property that no node carries is an `ANALYZE_ERROR`**, not an
empty column. Same for an edge type that does not exist at the commit you are
querying. Use `CALL db.propertyTypes()` and `CALL db.edgeTypes()` to see what
exists — but note they read a registry that remembers names from earlier builds,
so they over-report; confirm with a `count()` before relying on one.

**Variable-length traversal cannot be restricted to an edge type.** The
quantifier is postfix — `-[e]->{1,8}`, `-[e]->+`, `-[e]->*` — and adding a type
is rejected. That is why `cod_contacts_v2` exists: in a graph whose only edges
are hydrogen bonds, an untyped quantifier *is* a hydrogen-bond traversal.

```cypher
MATCH (a:Atom {uid:'2229029_O1'})-[e]->{1,6}(b:Atom) RETURN count(b)
```

**`IN` does not work, but `UNWIND` does** — and it is the replacement:

```cypher
UNWIND [1, 2] AS k MATCH (s:Structure) WHERE s.net_dim = k RETURN count(s)
```

**Useful and easy to miss:** `SKIP`, numeric comparisons in `WHERE`, arithmetic
in `RETURN` (`RETURN r.length * 2`), `labels(n)`, `count(*)`, multi-key
`ORDER BY`, `ORDER BY <alias>`, and `cosine_similarity()` /
`euclidean_distance()` over embedding properties.

**`toInteger()` and `toFloat()` do not work** despite appearing in the Cypher
surface — both are an `ANALYZE_ERROR` here.

---

## 5. Worked questions

### Which space groups dominate?

No GROUP BY, so project flat and tally:

```bash
crystal run spacegroups
```

```cypher
MATCH (s:Structure)-[:IN_SPACE_GROUP]->(g:SpaceGroup) RETURN g.hm_symbol
```

`hm_symbol` exists on **both** labels and means different things, which is the
single easiest way to get a wrong answer here.

`SpaceGroup` is keyed by group **number** — one node per group — and its
`hm_symbol` is whichever setting happened to be seen first. So this returns 0,
because the node for No. 14 carries `P 1 21/n 1`:

```cypher
MATCH (s:Structure)-[:IN_SPACE_GROUP]->(g:SpaceGroup)
WHERE g.hm_symbol = 'P 1 21/c 1' RETURN count(s)      -- 0, and not what you meant
```

Ask by **number** when you want the group, and use `Structure.hm_symbol` when
you want the setting as the depositor reported it:

```cypher
MATCH (s:Structure)-[:IN_SPACE_GROUP]->(g:SpaceGroup)
WHERE g.number = 14 RETURN count(s)                    -- 29,640, all settings

MATCH (s:Structure) WHERE s.hm_symbol = 'P 1 21/c 1'
RETURN count(s)                                        -- 18,643, that setting
```

For the same reason, `crystal run spacegroups` tallies by group and labels each
bucket with one representative symbol: the 29,640 printed against `P 1 21/n 1`
is all of No. 14, not that setting alone. `cod_versioned` keys `SpaceGroup` by
symbol instead, so there the settings are separate nodes.

### The hydrogen-bond network of one structure

```cypher
MATCH (a:Atom)-[r:CONTACT]->(b:Atom)
WHERE a.cod_id = 2229029 AND r.kind = 'hbond'
RETURN a.label, b.label, r.length, r.angle, r.symop_triplet
```

Every row carries the operation that generated the neighbour, so the periodic
network is reconstructible without building a supercell.

### R²₂(8) carboxylic-acid dimers

The canonical graph-set motif. Walk molecule → atom → contact → atom → molecule,
and require the generating operation to be an involution:

```cypher
MATCH (f1:Fragment)<-[:HAS_FRAGMENT]-(c1:Component)<-[:IN_COMPONENT]-(a1:Atom)
      -[h:CONTACT]->(a2:Atom)-[:IN_COMPONENT]->(c2:Component)
      -[:HAS_FRAGMENT]->(f2:Fragment)
WHERE f1.fragment_type = 'carboxylic_acid'
  AND f2.fragment_type = 'carboxylic_acid'
  AND h.kind = 'hbond' AND h.h_inferred = false
  AND h.is_involution = true
  AND a1.element = 'O' AND a2.element = 'O'
  AND a1.cod_id = a2.cod_id
RETURN a1.cod_id, a1.label, a2.label, h.symop_triplet, h.length, h.angle
```

Swap `is_involution = true` for `false` and the same query returns C(4)
catemers instead — screw axes and glides rather than inversion centres.

`a1.cod_id = a2.cod_id` is redundant — `a1` and `a2` are bound by the same
`CONTACT` edge, which never crosses a structure — but it is kept as an explicit
reminder that `Component` nodes *are* deduplicated corpus-wide, so any pattern
that reaches two atoms through two Component nodes rather than through one edge
does need scoping.

### Acid versus pyridine: which synthon wins?

```bash
crystal run synthon-homo      # O-H...O acid to acid
crystal run synthon-hetero    # O-H...N acid to pyridine
```

Both counts are stored *edges*. To compare physical hydrogen bonds, count
involution-generated homosynthon edges twice — a centrosymmetric acid dimer is
two bonds held as one edge, and the heterosynthon has no such pairing. On this
corpus the correction moves the split from 47.9 / 52.1 to 63.4 / 36.6 and
reverses the winner.

### Is a packing a dimer, a chain, a sheet or a framework?

Precomputed at ingest as `net_dim` (strong bonds) and `net_dim_weak` (weak
admitted as well): `0` finite motif, `1` chain, `2` sheet, `3` framework.

`-1` means **not scored** — the structure has no located-hydrogen strong
hydrogen bond, or the orbit expansion exceeded its ceiling. That is **44,486 of
84,801 structures, 52.5% of the corpus**, so any dimensionality percentage is
over the 40,315 that were scored, not over the whole corpus. Filter on
`has_net_dim = true` to be explicit about it. (`-2` is reserved for a
computation that raised; it is currently empty.)

```cypher
MATCH (s:Structure) WHERE s.net_dim = 2 RETURN count(s)
```

`net_dim_is_framework` marks structures whose *covalent* net is already periodic
— coordination polymers, where the number describes the framework rather than
the hydrogen-bond net. Exclude them if that matters to your question.

### Where else does this molecule appear?

```cypher
MATCH (c:Component)<-[:CONTAINS_COMPONENT]-(s:Structure)
WHERE c.inchikey = 'WPYMKLBDIGXBTP-UHFFFAOYSA-N'
RETURN s.cod_id, s.hm_symbol, s.year
```

That one returns **82 structures across 13 different space-group settings** —
one node, 82 edges, and the polymorphism falls out of the same traversal. In a
relational schema this is a self-join over a structure table on a chemical
identity string, which only works if that string was computed consistently in
the first place, and still says nothing about the packing without a second pass.

`crystal run recurrence` ranks molecules by how many structures they appear in.

---

## 6. The browser studio

<http://localhost:8087>, then pick a crystallography graph.

**Left panel** — corpus overview with the three contact populations separated, a
colour legend for the nine node types, and the full record of whatever node is
selected on the canvas.

**Investigate panel** (top right) — a library of queries on three shelves:

- *Packing & motifs* — the contact network itself
- *Solid form & design* — salt vs co-crystal, coformers, polymorphs
- *Provenance & versioning* — the commit history

Each entry says what it demonstrates, runs real Cypher, paints the returned
subgraph, and reports nodes, edges and latency. **show Cypher** exposes the
query; **edit & run this query** drops it into the input box so you can change a
predicate and see the canvas move.

**The input box takes Cypher directly.** Anything beginning `MATCH`, `CALL`,
`RETURN` or similar is executed; everything else routes to the nearest canned
investigation. A query projecting the eight-column subgraph shape

```
RETURN a, labels(a), <a display>, e, edgeType(e), b, labels(b), <b display>
```

paints the canvas. Anything flatter — a count, an average, a projection — comes
back as a table. Engine errors are shown verbatim, because `PARSE_ERROR`,
`ANALYZE_ERROR` and `PLAN_ERROR` mean quite different things.

The standard visualizer toolbar is hidden on these graphs on purpose: its data
pipeline fetches every node and edge in the graph, which is not viable at 4.7 M
atoms, and it would overwrite the subgraph the studio paints.

---

## 7. Time travel

The corpus is ingested chronologically, one commit per publication year: 26
years, and 26 entries in the `cod_versions` ledger.

```bash
crystal commits                  # year, tag, totals, commit hash
crystal at 2009 "MATCH (s:Structure) RETURN count(s)"      # 32,419
crystal at 2026 "MATCH (s:Structure) RETURN count(s)"      # 84,801
```

In the studio, five investigations carry a strip of 26 ticks below the answer;
clicking one re-runs the same query against that commit.

Over REST, two steps — a commit must be made resident before it can be read:

```bash
curl -s -X POST "http://localhost:6691/query?graph=cod_versioned" \
     -H "Content-Type: text/plain" -d "LOAD COMMIT '<hash>'"
curl -s -X POST "http://localhost:6691/query?graph=cod_versioned&commit=<hash>" \
     -H "Content-Type: text/plain" -d "MATCH (s:Structure) RETURN count(s)"
```

`LOAD COMMIT` is idempotent.

Two properties of the versioning worth knowing:

- **A commit survives a restart; an open change does not.** Anything built on an
  unsubmitted change has to be re-created, and there is no way to list changes.
- **Commit hashes are build-time values.** Rebuilding the same graph from the
  same script produces different hashes, so never hardcode one. Resolve it at
  runtime from the `cod_versions` ledger.
- **`CALL db.history()` returns 54 entries, not 26.** Each publication year
  costs two commits — an intermediate `COMMIT` so that the year's edge writes
  can see the nodes they attach to, then the `CHANGE SUBMIT` — plus the initial
  one. The 26 in the ledger are the ones worth checking out; use the ledger
  rather than picking entries out of the history.

**What is versioned.** `cod_versioned` carries `Structure`, `SpaceGroup`,
`Component` and `Fragment`, joined by `IN_SPACE_GROUP`, `CONTAINS_COMPONENT` and
`HAS_FRAGMENT`. It deliberately holds **no atoms and no contacts** — that layer
is 5.4 M edges and committing it 26 times would multiply the corpus by an order
of magnitude for no gain in the question it answers.

So structure counts, space-group distributions and fragment censuses rewind;
hydrogen-bond geometry, dimer counts and dimensionality do not. Asking for them
at a commit is an honest error rather than a wrong number:

```
$ crystal at 2009 "MATCH ()-[r:CONTACT]->() RETURN count(r)"
error: ANALYZE_ERROR: Unknown edge type: CONTACT
```

TuringDB has commit hashes, integer change ids and a single `main` — no tags and
no branches. The `cod@<year>` names live in the `cod_versions` ledger graph,
which makes the mapping itself queryable.

---

## 8. How the data gets in

```
COD rsync + MySQL  →  parse cache  →  JSONL  →  LOAD JSONL  →  cod_slice_v2
      download.py      build_graph.py          load_turingdb.py
```

### Acquisition

`ingest/download.py` fetches CIFs from `rsync://www.crystallography.net/cif/`
with `--files-from`, so it pulls the ~2 GB slice rather than the 109 GB tree,
and metadata from COD's public read-only MySQL mirror. It writes a manifest
recording the release revision, the SVN revision at download time, the exact
journal strings matched, the size cap, and the sorted ID list with a checksum.

### Parsing

`ingest/build_graph.py` parses each CIF with gemmi, then per structure:

1. builds atoms from the asymmetric unit;
2. expands symmetry and searches for neighbours (`periodic.py`);
3. detects covalent bonds and finds connected components — real molecules and
   ions, after expansion (`structure.py`);
4. detects hydrogen, weak and halogen contacts (`contacts.py`);
5. perceives identity and functional groups with RDKit (`perception.py`);
6. computes periodic-net dimensionality (`netdim.py`).

Results are cached per structure, so `--from-cache` re-emits the graph without
re-parsing. Bond-order perception runs in a killable child process
(`rdkit_guard.py`) because RDKit's search does not always terminate and is a C++
call no Python watchdog can interrupt.

### Loading

The graph is written as JSONL in Neo4j APOC's `apoc.export.json.all` shape — one
object per line, nodes and relationships interleaved:

```json
{"type":"node","id":"0","labels":["Structure"],"properties":{"cod_id":1100119}}
{"type":"relationship","id":"0","label":"HAS_SITE","start":{"id":"0"},"end":{"id":"1"},"properties":{}}
```

**Node ids and relationship ids must each start at 0 and increment with no
gaps.** A crc32-style identifier has to be renumbered into a dense index at
export; keep the original as an ordinary property.

The file goes in `<turing-dir>/data/`, then:

```cypher
LOAD JSONL 'cod_slice_v2.jsonl' AS cod_slice_v2
```

Two practical notes. `LOAD JSONL` **only ever creates** — loading onto an
existing graph is an `EXEC_ERROR`, and there is no `DROP GRAPH`, so rebuilding
means stopping the server and removing `<turing-dir>/graphs/<name>`. And the
statement must be issued with `?graph=` pointing at an *existing* graph;
targeting the name being created is `GRAPH_NOT_FOUND`.

Serialise with `allow_nan=False`. Python's `json.dumps` emits a bare `NaN` by
default, which is not valid JSON, and one occurrence rejects the entire file —
the API reports only `Failed to load JSONL graph`, with the line and column in
`<turing-dir>/logs/`.

### The two write paths, and how to choose

| | `LOAD JSONL` | Cypher `CHANGE NEW … COMMIT … CHANGE SUBMIT` |
|---|---|---|
| Same 84,801 structures | **55 s** | **77 min** |
| History | none — one immutable graph | 26 queryable commits |
| Cost profile | linear | quadratic in corpus size |

The chronological build is slower because each year's writes `MATCH` against a
graph that grows, and the dialect has no property index on that graph. Pick by
whether you need the history, not by preference. `ingest/build_versioned.py`
does the second.

### Property indexes

```cypher
CREATE INDEX ix_frag_type FOR (n) ON n.fragment_type
CREATE INDEX ix_contact_kind FOR [e] ON e.kind
```

These are **writes** — they must run inside a change. `CALL db.showIndexes()`
lists them. The optimiser rewrites `WHERE n.prop = <value>` into an index lookup
automatically. They are worth having, but see the next section: they are a
tiebreaker, not the main lever.

---

## 9. Writing fast queries

**Write a multi-hop read as one connected chain, starting at the most selective
label.** This is worth more than everything else combined.

Slow — separate patterns joined in the `WHERE`, which forces a cross product:

```cypher
MATCH (a1:Atom)-[h:CONTACT]->(a2:Atom),
      (a1)-[:IN_COMPONENT]->(c1:Component)-[:HAS_FRAGMENT]->(f1:Fragment),
      (a2)-[:IN_COMPONENT]->(c2:Component)-[:HAS_FRAGMENT]->(f2:Fragment)
WHERE f1.fragment_type = 'carboxylic_acid' AND f2.fragment_type = 'carboxylic_acid'
```

Fast — one chain, beginning at `Fragment`, of which there are 16:

```cypher
MATCH (f1:Fragment)<-[:HAS_FRAGMENT]-(c1:Component)<-[:IN_COMPONENT]-(a1:Atom)
      -[h:CONTACT]->(a2:Atom)-[:IN_COMPONENT]->(c2:Component)
      -[:HAS_FRAGMENT]->(f2:Fragment)
WHERE f1.fragment_type = 'carboxylic_acid' AND f2.fragment_type = 'carboxylic_acid'
```

Same rows. On this corpus the first does not return at all — it was stopped at
900 s — and the second runs in **157 ms**. At one-eighth the corpus size the
same pair is 17.2 s against 0.84 s, so the penalty grows faster than the data.
Property indexes on the slow form bought 30%; the rewrite is worth four orders
of magnitude.

The planner follows the pattern as written. Start from whichever label has the
fewest nodes and chain outward.

**Other things that help:**

- Filter on `kind` early: it is indexed (`CALL db.showIndexes()` lists what is).
  `h_inferred` is not indexed, but it is cheap once `kind` has cut the set.
- Prefer the precomputed property to recomputing: `net_dim` and `is_involution`
  exist so that dimensionality and ring-vs-chain cost nothing at query time.
- Aggregates over large edge sets are the slowest common operation; `avg()` over
  152,592 edges takes ~1.1 s where every traversal here is under 250 ms.
- For exploration, use `CALL db.labels()`, `CALL db.propertyTypes()` and
  `CALL db.edgeTypes()` rather than `MATCH (n) RETURN n`.

### `shortestPath`

It works, and for a contact network it is the obvious thing to reach for:

```cypher
MATCH (a:Atom), (b:Atom) shortestPath(a, b, w, dist, path) RETURN dist
```

**Do not project an endpoint variable.** `RETURN a.label` — a String property of
an endpoint — terminates the server process. `RETURN a` and `RETURN a.cod_id`
are a clean `PLAN_ERROR`; only `RETURN dist` and `RETURN dist, path` are safe.
`path` is a positionally interleaved list of node and edge ids whose two id
spaces overlap, so resolve the even positions with `CALL db.getNodes`.

Given that `label` is a String on every `Atom` here, this is easy to hit. Treat
it as a sharp edge rather than a feature until it is fixed upstream.

---

## Checking your changes

Two gates run the queries the project ships against a live server, and both
fail loudly rather than returning empty:

```bash
python tests/check_cli_queries.py
python tests/check_frontend_queries.py --negative-control
```

The front-end gate recovers queries by scanning the source, so it also asserts
that a set of required schema elements — `CONTACT`, `IN_COMPONENT`,
`HAS_FRAGMENT`, `net_dim`, `is_involution`, `Snapshot`, `IN_SPACE_GROUP` —
appears somewhere in what it extracted, and fails if any is missing. Without
that, a scraper that quietly stops matching reports success over an empty set.

It also checks that every `const [...] = await Promise.all([...])` names exactly
as many variables as the array has entries: the panels read their results
positionally, so a mismatch renders a plausible wrong number rather than an
error.

```bash
python tests/corpus_facts.py     # re-derive every figure the README quotes
```
