# Open questions

Per the ground rules: anything I could not resolve from the docs, the SDK source,
or a running instance goes here rather than being guessed at. Scientific
judgement calls are flagged for a chemist.

Status key: **BLOCKING** (need an answer before I build the affected stage),
**DECIDED** (resolved, recorded for the walkthrough), **OPEN** (proceeding on a
stated assumption that should be reviewed).

---

## A. TuringDB capability gaps

### Q-1 — Variable-length traversal cannot be restricted to an edge type. **BLOCKING → proposed answer**

The engine rejects `-[e:CONTACT]->{1,8}` with
`Edge type filters are not supported with variable-length paths`. Untyped
quantifiers work fine. Q2 ("traverse the hydrogen-bond network 8 hops") therefore
cannot be expressed against a graph that also contains covalent bonds between the
same `Atom` nodes, because an untyped quantifier would walk both.

**Proposal**: materialise the contact network as its own graph, `cod_contacts` —
`Atom` nodes plus hydrogen-bond `CONTACT` edges only — so an untyped quantifier
*is* a contact traversal. The full-schema graph `cod_slice` keeps everything for
Q1/Q3/Q4 and Explore. Neo4j is loaded from the identical file, so the benchmark
stays like-for-like.

Cost: an extra graph, and the Explore screen's depth slider queries a different
graph than the record panel. Needs sign-off because it is a visible modelling
decision a CCDC engineer will ask about.

### Q-2 — Should the demo run on released v1.37 or on `origin/main`? **BLOCKING**

Released `v1.37` (2026-08-21) supports 26/40 of the constructs the demo wants.
`origin/main` is **247 commits ahead** and its log shows `WITH` projections,
`collect()`, multi-part queries, `CALL ... YIELD` over hops, and filtering on
unwound variables — i.e. most of what is currently missing.

Running on main would make the demo Cypher look far more like ordinary Cypher.
Running on v1.37 is what CCDC could actually install today. This changes the
honesty of the "your team won't have to rewrite anything" claim in opposite
directions and needs a decision, not a default.

### Q-3 — There are no branches or tags. **BLOCKING → proposed answer**

Stage 4 asks for tags `cod@<year>` and branches `curated` / `experimental-only`.
Neither exists: the docs never mention tags, `LIST BRANCH` / `CREATE TAG` /
`CHECKOUT <branch>` are parse errors, and the SDK has no branch or tag method.
The only primitives are integer change IDs, commit hashes, and a single `main`.

**Proposal**: keep one commit per publication year (that part works), and
maintain our own ledger mapping `cod@<year>` -> commit hash, stored as a small
second graph so it is itself versioned and queryable rather than a JSON file on
disk. `checkout(commit=<hash>)` then gives real time travel, and the History
screen shows a real commit log from `CALL db.history()`.

For the two "branches", the honest framing is that they are two separate graphs
built from the same ingest, not two refs on one history. I would rather say that
plainly on the History screen than imply a branching model that does not exist.

### Q-4 — `IS NULL` / `IS NOT NULL` do not work. **DECIDED**

`WHERE s.temperature IS NOT NULL` fails with
`Operands are not valid or compatible types: 'Double' and 'Null'`.
COD is full of missing fields and the spec forbids fabricating them.

**Decision**: write an explicit boolean companion for each optional numeric field
(`has_temperature`, `has_pressure`, `has_r_factor`, `has_z_prime`, …). Absent
properties still project safely as nulls and are silently excluded by
comparisons, so nothing is lost; the flag just makes "what is missing?"
answerable. The ingest report counts missing fields either way.

### Q-5 — Aggregation cannot group. **DECIDED**

Only `count()` and `avg()` exist, and only as a *single* return item, so
`RETURN f.fragment_type, count(c)` is rejected. Q4's motif-frequency table and
Q3's `support` ranking are both group-by shaped.

**Decision**: issue one `count()` query per fragment type per commit and assemble
the table client-side. For a 12-motif table across 5 commits that is 60 cheap
queries. The API reports the summed server-side execution time so the latency
claim stays honest rather than hiding 60 round trips behind one number.

### Q-6 — Q1's cycle cannot be written as a cycle. **DECIDED**

`MATCH (a1)-[h1]->(a2), (a2)-[h2]->(a1)` fails with
`PLAN_ERROR: Loop detected. This is not supported yet`.

**Decision**: express the reciprocal pair as two independent edge patterns joined
on properties —
`MATCH (a1)-[h1:CONTACT]->(a2), (a3)-[h2:CONTACT]->(a4) WHERE a1.uid = a4.uid AND a2.uid = a3.uid`
— verified to return the correct pairs. Documented in `docs/DIALECT.md`.

### Q-7 — No string matching. **OPEN**

`STARTS WITH` and `CONTAINS` both fail with
`String expressions are currently not supported`, and `IN` is unsupported.
The Explore screen is specified to "search structures by formula".

**Assumption**: support exact-match on a normalised formula string plus
element-set search via the existing `(Structure)-[:CONTAINS_ELEMENT]->(Element)`
edges, which is the graph-native way to do it anyway. Free-text substring search
over formulae would have to happen in the API layer over a cached list. Flagging
because it slightly narrows what the Explore search box can do.

### Q-8 — `CALL db.getNodes([...])` crashes the server. **DECIDED**

On the older source build (`3c113474b`) this kills the process; every subsequent
query gets `Connection refused`. We do not use that build, and the demo does not
call the procedure. Recording it because the stock TuringDB visualizer's data
layer *does* call it — which is a reason the demo ships its own front end rather
than embedding the visualizer.

---

## B. Data / provenance

### Q-9 — The slice is ~89k structures, not the 30–50k the spec estimated. **OPEN**

Live counts from COD's own API under its default filter:

| Journal | Structures |
|---|---|
| Acta Cryst. E | 44,773 |
| CrystEngComm | 31,184 |
| Crystal Growth & Design | 13,841 |
| **Total** | **~89,800** |

That is still comfortably interactive, and richer for Q1/Q3. **Assumption**: take
all three in full rather than sampling, since a bigger slice makes the coformer
query less thin. Say if you would rather cap it.

### Q-10 — The COD release tarball is 19 months stale. **DECIDED**

`LAST_RELEASE.txt` reports `297631 2025.02.09`; there is no 2026 release. The
rsync tree, by contrast, is live (newest file mtimes are today).

**Decision**: acquire CIFs over `rsync://www.crystallography.net/cif/` (resumable
by nature, and `--delete` reflects retractions) and metadata from the live
read-only MySQL at `cod_reader@sql.crystallography.net`. The manifest records the
`LAST_RELEASE.txt` revision, the `svn info` revision at download time, and the
per-row `data.svnrevision`, so provenance is exact even though we are not using a
numbered release. COD's `robots.txt` is `Disallow: /`, so no HTTP scraping.

### Q-11 — 151 CIFs exist on disk with no metadata row. **OPEN**

The rsync tree holds 535,041 CIFs; COD's homepage reports 534,890 entries. The
gap is probably on-hold or retracted records. **Assumption**: treat file-without-
metadata as a skip, and count it in the ingest report rather than silently
dropping it.

---

## C. Chemistry — for review by a chemist before the demo

### Q-12 — Which covalent radii table? **OPEN**

The spec says "sum of covalent radii plus a tolerance of 0.4 Å" and requires the
table be cited. **Assumption**: Cordero et al., *Dalton Trans.*, 2008, 2832
(the standard modern set, and what gemmi ships). Alternative would be Pyykkö &
Atsumi 2009. Flagging because the choice changes borderline metal–ligand bonds,
which is exactly where the metal-organics will be decided.

### Q-13 — Which van der Waals radii for the halogen-bond criterion? **OPEN**

The spec sets the X···A cutoff at "the sum of van der Waals radii" without naming
a set. **Assumption**: Bondi 1964 with Rowland & Taylor 1996 updates — the
convention most crystal-engineering papers use. Needs confirming, because Bondi
has no value for several elements and the fallbacks differ between tables.

### Q-14 — Should the π-stacking criterion use ring centroids only? **OPEN**

The spec gives centroid–centroid < 4.0 Å and interplanar angle < 30°. That admits
badly offset stacks that most crystal engineers would not call π-stacking; the
usual extra constraint is a centroid-offset / slippage limit (commonly ≤ 2.0 Å)
or a perpendicular-distance criterion. **Assumption**: implement exactly what the
spec says, and additionally record the offset on the edge so a reviewer can
tighten the threshold without re-running the ingest. Not adding an undocumented
cutoff.

### Q-15 — `h_inferred` fallback distance. **OPEN**

The spec sets the heavy-atom D···A fallback at < 3.5 Å when H positions are
absent. That is a common convention, but it is generous for N–H···O and tight for
S–H···O. **Assumption**: use 3.5 Å uniformly as specified, always set
`h_inferred: true`, and never mix the two populations in a query without the
flag being visible in the UI. The ingest report will state what fraction of
hydrogen bonds are inferred, per journal, because that number will be large for
Acta E and CCDC will ask for it.

### Q-16 — What counts as "solvent" and "principal" for `role`? **OPEN**

`role` on `CONTAINS_COMPONENT` is what stops the coformer query returning water
for everything. **Assumption**: classify by a curated solvent InChIKey list
(water, the common alcohols, DMSO, DMF, MeCN, chlorinated solvents, THF, ethers,
common counter-ions) plus a heuristic that the largest non-solvent component by
heavy-atom count is `principal` and remaining non-solvent, charged components are
`counter_ion`. The solvent list will live in one reviewable table like the SMARTS
patterns. This is a chemical judgement call and I would like it reviewed.

---

## D. Product / framing

### Q-17 — "In the TuringDB visualizer" vs. the Stage 8 spec. **BLOCKING**

The brief opens with "build a crystallography demo in the turingDB visualizer",
but Stage 8 specifies a standalone React + Vite app in `web/` with Cytoscape.js,
a 3D packing view, and a two-column diff. Those are different deliverables. The
existing demos on this box are built by grafting panels onto a fork of the stock
visualizer; the Stage 8 screens (especially the benchmark opener and the version
diff) do not fit that shell well, and the stock visualizer's data layer calls a
procedure that is absent or crashing on these builds (Q-8).

**Assumption**: build the standalone app per Stage 8. Confirm.

---

## E. Reframing after the 2026-09-03 steer

The brief was narrowed: no Neo4j, no benchmark, graph size is not the point.
What matters is that the demo tells a real crystallographic story and shows why
this data wants to be a graph. Recorded here so the reasoning survives.

### Q-18 — What actually makes the graph case, if not speed? **DECIDED**

Three things, in order of how convincing a crystallographer will find them:

1. **The contact network is an edge set nobody persists.** COD and the CSD store
   atoms and coordinates. Hydrogen bonds, halogen bonds and pi-stacks are
   *recomputed geometrically* every time anyone asks. We compute them once, at
   ingest, with the symmetry operation attached, and then a packing question is
   a pattern match instead of a geometry job over every candidate structure.

2. **The interesting motifs are cycles and paths, which is what SQL is worst at.**
   A carboxylic acid dimer is a closed loop of two hydrogen bonds between two
   molecules. Network dimensionality (see Q-19) is reachability. Neither has a
   natural relational form; both are one-liners over a contact graph.

3. **Identity is cross-structure.** The same molecule appearing in 40 structures
   is a node with 40 edges, not 40 unrelated rows joined on a string. That is
   what makes polymorph, solvate and coformer questions expressible at all.

### Q-19 — Hydrogen-bond network dimensionality as the headline query. **DECIDED**

Seed on one atom, walk the hydrogen-bond network outward, and count how many
atoms are reachable at each depth. The *shape of that growth curve* tells a
crystallographer something they genuinely care about:

| Growth | Motif | Why it matters |
|---|---|---|
| saturates at 2-4 atoms | isolated dimer (0D) | typically higher solubility |
| roughly linear | chain (1D) | often needle morphology, anisotropic |
| roughly quadratic | sheet (2D) | plate morphology, easy cleavage, tabletting |
| roughly cubic | framework (3D) | hard, less soluble, humidity-stable |

Morphology, tabletting behaviour, solubility and hygroscopicity all follow from
this, so it is a real result rather than a graph-database party trick. It is
also exactly a traversal, which is the thing a relational schema cannot do
without a recursive CTE over a table that does not exist yet.

Implementation note: TuringDB rejects an edge-type filter on a variable-length
path (Q-1), so this runs against a contact-only projection of the graph where an
untyped quantifier *is* a hydrogen-bond traversal.

### Q-20 — Synthon competition, the query a crystal engineer would ask. **OPEN**

When a molecule carries both a carboxylic acid and a pyridine nitrogen, which
motif actually forms in the crystal: the acid-acid homosynthon, or the
acid-pyridine heterosynthon? This is a live question in co-crystal design and
the answer is a frequency count over the contact graph conditioned on which
fragments are present. Worth building if the fragment perception rate holds up.

**Assumption**: report it as observed counts with the `h_inferred` population
excluded, since a motif claim based on inferred hydrogen positions would not
survive scrutiny.
