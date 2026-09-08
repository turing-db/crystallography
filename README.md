# Crystallography on TuringDB

A demo built on the **Crystallography Open Database** (COD, CC0) that argues one
thing: crystal-structure data is natively a graph, and the most valuable layer
of it — the intermolecular contact network — is the layer nobody persists.

COD and the CSD store atoms and coordinates. Hydrogen bonds, halogen bonds and
molecular connectivity are *recomputed geometrically* every time somebody asks a
question. This demo computes them once, at ingest, with the generating symmetry
operation attached to every edge, and then packing questions become pattern
matches instead of geometry jobs.

It also uses TuringDB's native commits: the corpus is ingested chronologically,
one commit per publication year, so `cod@2010` **is** the literature as it stood
at the end of 2010. A statistic published then still reproduces, without anyone
archiving a database dump.

---

## Quick start

```bash
./run.sh            # start TuringDB + the UI (data already built)
./run.sh --ingest   # also acquire COD and build the graphs from scratch
```

Then open <http://localhost:8087>.

Requirements: `uv`, Node 20+, and `rsync`. The `--ingest` path needs network
access to `crystallography.net`. Nothing else is assumed — the TuringDB server
binary ships inside the `turingdb` Python wheel, so there is no separate install.

---

## What's in the graph

Built from CrystEngComm, Crystal Growth & Design and Acta Cryst. E — the three
journals closest to CCDC's small-molecule remit.

| | |
|---|---|
| Structures | 9,899 |
| Atomic sites | 547,041 |
| Components (molecules & ions) | 10,766 |
| Covalent bonds | 584,812 |
| **Intermolecular contacts** | **33,930** |
| Space groups | 159 |

Mean H···A distance **2.09 Å** at a mean D–H···A angle of **160.5°** — textbook
strong-hydrogen-bond geometry, which is the first sanity check a crystallographer
will run.

### Graphs on the server

| graph | what it is |
|---|---|
| `cod_slice_v2` | full schema: Structure, Atom, Component, Fragment, SpaceGroup, Element, Publication, Author, Journal |
| `cod_contacts_v2` | atoms + located-H hydrogen bonds only — the traversable projection (see *Known limits*) |
| `cod_versioned` | the same corpus committed chronologically, 26 commits, 2001→2026 |
| `cod_versions` | the tag ledger: `cod@<year>` → commit hash |

---

## The demo

One studio page, reached from the flask icon in the left rail.

**Left panel** — corpus overview, a colour legend for the nine node types, and
the full record of whatever node is selected on the canvas.

**Investigate panel** (top right) — nine investigations. Each runs real Cypher,
paints the returned subgraph, reports nodes/edges/latency, exposes the Cypher,
and streams a narrative answer. Free text routes to the nearest investigation.

Five of them carry a **commit toggle**: a strip of 26 ticks, one per publication
year, with bar height encoding corpus size. Click one and the same query re-runs
against the graph as it stood that year.

### Suggested walkthrough

1. **"How are space groups distributed in this corpus?"** — the opener. Purple
   `SpaceGroup` hubs with blue `Structure` spokes, cluster labels naming the big
   ones. Lands the textbook result: P2₁/c 34.9%, P-1 26.1%, centrosymmetric
   groups 74.0%.
2. **Now click the commit ticks.** At `cod@2005` the corpus is 1,038 structures
   and P2₁/c is 21.8%; at HEAD it is 9,899 and 34.9%. P2₁/c's dominance *grew*
   over the period. This is the versioning story in one gesture — no archived
   dumps, the corpus itself rewinds.
3. **"Which functional groups dominate the corpus?"** — the densest view, one
   golden `Fragment` hub per group. 17,152 occurrences over 14 types. Mention
   that 465 components carry a carboxylic acid and 565 a pyridine nitrogen:
   that pair is the acid/pyridine heterosynthon of co-crystal design.
4. **Click a node** — the left panel reads the record back. A Structure gives
   cell parameters, space group, Z, Z′, R factor and temperature; a Component
   gives formula, InChIKey and solvent status.
5. **"Show the hydrogen-bond network of one structure"** — the contact layer
   itself. Every edge carries the symmetry operation that generated the
   neighbour, in CIF `2_565` form, which is what makes the periodic network
   traversable without materialising a supercell.
6. **"If a structure is later corrected, which analyses are suspect?"** — the
   compliance close. COD 2229029 enters at `cod@2011` and sits in 16 of 26
   commits: exactly the set of published analyses to re-examine.

---

## Chemistry: what the criteria are

Every cutoff lives in [`ingest/chemistry.py`](ingest/chemistry.py) with its
literature source, and the SMARTS patterns in
[`ingest/fragments.py`](ingest/fragments.py), so a chemist can review both in
one place.

- **Covalent bonds** — `d < r_cov(A) + r_cov(B) + 0.40 Å`, radii from
  Cordero et al., *Dalton Trans.* 2008, 2832. Sites below 0.5 occupancy are
  excluded so disordered partial sites are not bonded together.
- **Hydrogen bonds** — H···A < 2.5 Å with D–H···A > 120°, donors and acceptors
  from {N, O, F, S, Cl}. Where no hydrogen was refined, a heavy-atom D···A
  < 3.5 Å fallback is used and the edge is flagged `h_inferred`. The two
  populations are never silently mixed.
- **Halogen bonds** — C–X···A with X ∈ {Cl, Br, I}, X···A below the sum of van
  der Waals radii (Bondi 1964, extended by Mantina 2009) and C–X···A > 150°.
- **Components** — connected components of the covalent graph *after symmetry
  expansion*, so they are real molecules and ions rather than whatever the CIF
  deposited. Identity is an InChIKey where RDKit perception succeeds, and a
  formula+connectivity digest otherwise, stored in a **separate property** so it
  is never mistaken for an InChIKey.

### Verification

Symmetry handling is the thing that would be spotted in seconds if it were
wrong, so it is tested rather than asserted:

```bash
uv run python tests/validate_symmetry.py 200   # symop round-trip
uv run python tests/validate_chemistry.py 400  # valence plausibility
uv run python tests/validate_components.py 10  # eyeball component perception
uv run python tests/probe_turingdb_syntax.py   # the Cypher surface
```

`validate_symmetry` re-derives the operation stored on each edge and re-applies
it: over 28,409 neighbour pairs, **100%** resolve and reproduce the neighbour's
position with a median residual of **1e-15 Å**.

`validate_chemistry` reports valence distributions. In purely organic
environments: H 98.2%, C 97.4%, N 99.6%, O 97.4%, S 97.5% plausible.
Metal-coordinated atoms are reported *separately* rather than scored against
organic valences, because a μ-carboxylate oxygen bridging two nickels genuinely
has three bonds.

---

## Known limits

Stated up front, because they are the questions a crystallographer will ask.

- **π-stacking is not implemented.** It needs ring perception, and a
  half-defined ring criterion would be worse than none. Hydrogen and halogen
  bonds are in.
- **InChIKey perception reaches ~40% of components.** Metal complexes fail by
  construction — InChI has no well-defined representation for them — and
  components above 60 atoms are skipped because RDKit's bond-order search does
  not terminate on them. Those keep the connectivity digest for identity.
- **Variable-length traversal cannot be restricted to an edge type.** TuringDB
  rejects `-[e:CONTACT]->{1,8}` outright, so the contact network is materialised
  as its own graph (`cod_contacts_v2`) where an untyped quantifier *is* a
  hydrogen-bond traversal.
- **Network dimensionality is a computation, not a traversal.** The stored
  contact graph is a *quotient* graph, so a chain through the crystal is a short
  cycle, not a long path — counting reachable nodes by depth reports everything
  as isolated. `queries/dimensionality_net.py` does it properly, via the rank of
  the lattice-translation subgroup generated by cycles.
- **The motif-drift numbers are not publishable from this corpus.** The
  cumulative series is flat by construction and the per-year cohorts are too
  thin at both tails (98 components in 2026). `queries/versioned_stats.py`
  prints a do-not-quote warning below a 400-component threshold. The
  *mechanism* is demonstrated; the finding would need the full 85k slice.
- **The narrative answers in the UI are not from a language model.** They are
  templated prose over live `count()`/`avg()` queries. Every number is real and
  re-derivable; nothing is invented.
- **Two space-group counts differ, deliberately.** `cod_slice_v2` keys a
  `SpaceGroup` node by group *number* (159 nodes); `cod_versioned` keys by
  Hermann-Mauguin *symbol* (195), so P2₁/c and P2₁/n are separate. Both are
  correct and answer different questions.

---

## Provenance

CIFs come from the live rsync tree at `rsync://www.crystallography.net/cif/`,
not the published tarball — the newest release is `cod-rev297631-2025.02.09`,
roughly 19 months stale. Metadata comes from COD's public read-only MySQL
mirror. `data/manifest/slice_manifest.json` records the release revision, the
metadata SVN revision, the exact journal-name strings matched, the size cap
applied, and the sorted COD ID list with a checksum.

Files above 512 kB are skipped (95.0% of the slice kept, 2.82 GB); the excluded
IDs are listed in `data/manifest/slice_skipped_ids.txt` rather than silently
dropped. COD's `robots.txt` is `Disallow: /`, so nothing is scraped over HTTP.

COD data is dedicated to the public domain under CC0.

---

## Layout

```
ingest/          acquisition and the ingest pipeline
  download.py        COD acquisition (rsync + MySQL) with an auditable manifest
  chemistry.py       radii tables and geometric criteria, each cited
  fragments.py       the SMARTS table
  periodic.py        explicit symmetry expansion + neighbour search
  symmetry.py        symop encoding in CIF form, and its verification
  structure.py       bonds and periodic connected components
  contacts.py        hydrogen and halogen bond detection
  perception.py      RDKit identity and fragment matching
  solvents.py        solvent / counter-ion classification
  build_graph.py     emits LOAD JSONL-shaped output
  project_contacts.py the atoms+H-bonds projection
  load_turingdb.py   load and verify
  build_versioned.py one commit per publication year + the tag ledger
queries/         the analytical queries
  dimensionality_net.py  periodic-net dimensionality (0D/1D/2D/3D)
  versioned_stats.py     the same statistic across commits, plus the audit trail
tests/           validation and dialect probes
docs/
  DIALECT.md         the verified TuringDB Cypher surface
  QUESTIONS.md       open questions, including chemistry calls for review
visualizer/      the front end (a trimmed TuringDB visualizer)
  src/components/viewer/crystal/   the studio: panel, chat, canvas, labels
```

`docs/DIALECT.md` is worth reading before writing any Cypher against this
engine — it records what the dialect actually accepts, tested against a running
instance, and the rewrite each query needed.

---

## Credits

`visualizer/` is a trimmed fork of
[turing-db/turingdb-visualizer](https://github.com/turing-db/turingdb-visualizer).
The WebGL canvas, node inspector, query bar and design system are theirs; this
repo adds `src/components/viewer/crystal/` and removes the studio pages that are
not about crystallography.

Structural data is from the **Crystallography Open Database**, dedicated to the
public domain under CC0. If you use COD in published work, cite
Gražulis et al., *J. Appl. Cryst.* **42** (2009) 726 and
*Nucleic Acids Res.* **40** (2012) D420.
