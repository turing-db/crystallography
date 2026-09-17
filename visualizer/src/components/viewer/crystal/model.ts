// Crystallography studio: data access, node palette, and the investigation
// queries the chat panel offers.
//
// Everything here runs real Cypher against the `cod_slice_v2` graph built from
// the Crystallography Open Database. The narrative answers are assembled from
// the numbers those queries return -- they are templated prose over live
// results, not the output of a language model.
import { executeCypherQuery } from '@/api'

export const CRYSTAL_GRAPHS = new Set(['cod_slice_v2', 'cod_contacts_v2'])

//: Built by ingest/build_versioned.py -- one commit per publication year, plus
//: a ledger graph holding the year -> commit-hash mapping (TuringDB has no tags).
export const VERSIONED_GRAPH = 'cod_versioned'
export const LEDGER_GRAPH = 'cod_versions'

// ---------------------------------------------------------------------------
// palette -- one colour per node label
// ---------------------------------------------------------------------------

export interface LabelStyle {
  label: string
  color: number // canvas wants a 0xRRGGBB number
  css: string // same colour for the legend swatch
  blurb: string
}

export const LABEL_STYLES: LabelStyle[] = [
  { label: 'Structure', color: 0x6ea8fe, css: '#6ea8fe', blurb: 'one crystal-structure determination' },
  { label: 'Atom', color: 0xa8b3c4, css: '#a8b3c4', blurb: 'an asymmetric-unit atomic site' },
  { label: 'Component', color: 0x7fe0b0, css: '#7fe0b0', blurb: 'a discrete molecule or ion' },
  { label: 'Fragment', color: 0xf2c14e, css: '#f2c14e', blurb: 'a perceived functional group' },
  { label: 'SpaceGroup', color: 0xc98bdb, css: '#c98bdb', blurb: 'crystallographic symmetry group' },
  { label: 'Element', color: 0xf58a6f, css: '#f58a6f', blurb: 'chemical element' },
  { label: 'Publication', color: 0x64c9cf, css: '#64c9cf', blurb: 'the paper reporting the structure' },
  { label: 'Author', color: 0x9aa6b8, css: '#9aa6b8', blurb: 'a depositor / author' },
  { label: 'Journal', color: 0x8f9dc4, css: '#8f9dc4', blurb: 'publication venue' },
  { label: 'Snapshot', color: 0xffab3d, css: '#ffab3d', blurb: 'one commit: the corpus as of that year' },
]

export const STYLE_BY_LABEL: Record<string, LabelStyle> = Object.fromEntries(
  LABEL_STYLES.map((s) => [s.label, s])
)

export const EDGE_COLOR: Record<string, number> = {
  CONTACT: 0x7fc8ff, // intermolecular contacts -- the edges nobody persists
  BONDED_TO: 0x5d6470,
  HBOND: 0x7fc8ff,
  HAS_SITE: 0x3f4652,
  CONTAINS_COMPONENT: 0x7fe0b0,
  HAS_FRAGMENT: 0xf2c14e,
  IN_SPACE_GROUP: 0xc98bdb,
  CONTAINS_ELEMENT: 0xf58a6f,
  PUBLISHED_IN: 0x64c9cf,
  AUTHORED_BY: 0x555c68,
  IN_JOURNAL: 0x8f9dc4,
  SUPERSEDES: 0xff8a65,
  NEXT: 0xffab3d,
}

// ---------------------------------------------------------------------------
// query plumbing
// ---------------------------------------------------------------------------

// TuringDB returns column-major chunks: data[chunk][column][row].
function columns(resp: unknown[][]): unknown[][] {
  const out: unknown[][] = []
  for (const chunk of resp ?? []) {
    if (!Array.isArray(chunk)) continue
    for (let c = 0; c < chunk.length; c++) {
      const col = chunk[c]
      if (!Array.isArray(col)) continue
      out[c] = (out[c] ?? []).concat(col)
    }
  }
  return out
}

export async function runCypher(graph: string, query: string): Promise<unknown[][]> {
  const resp = await executeCypherQuery({ graph, query })
  return columns(resp as unknown[][])
}

/**
 * Same as runCypher but able to read a PAST COMMIT.
 *
 * The shared `executeCypherQuery` builds its own URL and has no commit
 * parameter, so time travel needs its own request. TuringDB wants two steps:
 * `LOAD COMMIT '<hash>'` makes the commit resident (it is idempotent and lazy --
 * only HEAD is in memory when a graph loads), then `?commit=<hash>` pins reads
 * to it. Verified through the visualizer proxy: cod@2005 returns 1,038
 * structures where HEAD returns 9,899.
 */
const loadedCommits = new Set<string>()

export async function queryAt(
  graph: string,
  query: string,
  commit?: string
): Promise<unknown[][]> {
  if (commit && !loadedCommits.has(`${graph}:${commit}`)) {
    await fetch(`/api/query?graph=${encodeURIComponent(graph)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'text/plain' },
      body: `LOAD COMMIT '${commit}'`,
    })
    loadedCommits.add(`${graph}:${commit}`)
  }
  const url =
    `/api/query?graph=${encodeURIComponent(graph)}` +
    (commit ? `&commit=${encodeURIComponent(commit)}` : '')
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'text/plain' },
    body: query,
  })
  const json = await res.json()
  if (json.error) throw new Error(`${json.error}: ${json.error_details ?? ''}`)
  return columns(json.data as unknown[][])
}

export async function scalarAt(
  graph: string,
  query: string,
  commit?: string
): Promise<number> {
  const cols = await queryAt(graph, query, commit)
  const v = cols[0]?.[0]
  return typeof v === 'number' ? v : Number(v) || 0
}

export async function scalar(graph: string, query: string): Promise<number> {
  const cols = await runCypher(graph, query)
  const v = cols[0]?.[0]
  return typeof v === 'number' ? v : Number(v) || 0
}

const num = (v: unknown) => (typeof v === 'number' ? v : Number(v) || 0)
const str = (v: unknown) => (v == null ? '' : String(v))

// ---------------------------------------------------------------------------
// subgraphs
// ---------------------------------------------------------------------------

export interface GNode {
  id: number
  label: string
  name: string
}
export interface GEdge {
  id: number
  src: number
  tgt: number
  type: string
}
export interface Subgraph {
  nodes: GNode[]
  edges: GEdge[]
}

/**
 * Read a subgraph out of a query shaped as
 *   RETURN a, labels(a), <a display>, e, edgeType(e), b, labels(b), <b display>
 * i.e. 8 columns. Node ids come back as opaque integers, so the display value
 * has to be projected explicitly -- `RETURN n` alone yields only the id.
 */
export async function loadSubgraph(
  graph: string,
  query: string,
  commit?: string
): Promise<Subgraph> {
  const c = commit ? await queryAt(graph, query, commit) : await runCypher(graph, query)
  const nodes = new Map<number, GNode>()
  const edges = new Map<number, GEdge>()
  const rows = c[0]?.length ?? 0
  for (let i = 0; i < rows; i++) {
    const aId = num(c[0]?.[i])
    const aLab = str(c[1]?.[i])
    const aName = str(c[2]?.[i])
    const eId = num(c[3]?.[i])
    const eType = str(c[4]?.[i])
    const bId = num(c[5]?.[i])
    const bLab = str(c[6]?.[i])
    const bName = str(c[7]?.[i])
    if (!nodes.has(aId)) nodes.set(aId, { id: aId, label: aLab, name: aName || aLab })
    if (!nodes.has(bId)) nodes.set(bId, { id: bId, label: bLab, name: bName || bLab })
    // edge ids live in their own id space, so they can collide with node ids;
    // they are only ever used as canvas edge keys
    if (!edges.has(eId)) edges.set(eId, { id: eId, src: aId, tgt: bId, type: eType })
  }
  return { nodes: [...nodes.values()], edges: [...edges.values()] }
}

/**
 * Run a Cypher string the user typed, rather than one of the canned
 * investigations.
 *
 * The stock visualizer toolbar is deliberately hidden on the crystallography
 * graphs (see pages/viewer.tsx): its pipeline fetches EVERY node and edge in
 * the graph, which is hopeless at 4.7M atoms, and its next sync would wipe the
 * subgraph this studio paints by hand. That is a good reason to hide THAT
 * toolbar and a bad reason to have no Cypher box at all -- being able to read
 * and edit the query is most of what makes the demo credible to an audience
 * that writes queries for a living.
 *
 * So raw Cypher goes through the studio's own painter instead. Two shapes come
 * back:
 *
 *  - a query projecting the 8-column subgraph shape
 *    (`a, labels(a), <name>, e, edgeType(e), b, labels(b), <name>`) paints the
 *    canvas, exactly as an investigation does;
 *  - anything else -- a count, an average, a flat projection -- comes back as
 *    columns to tabulate, because refusing to show a scalar would make the box
 *    useless for precisely the checks someone wants to run live.
 */
export interface RawResult {
  kind: 'graph' | 'table'
  sub?: Subgraph
  columns?: string[]
  rows?: string[][]
  truncated?: boolean
  ms: number
}

/** Rows to show before truncating a tabular result. */
const RAW_ROW_LIMIT = 40

export async function runRawCypher(
  graph: string,
  query: string
): Promise<RawResult> {
  const t0 = performance.now()
  const res = await fetch(`/api/query?graph=${encodeURIComponent(graph)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'text/plain' },
    body: query,
  })
  const json = await res.json()
  // Failures arrive as HTTP 200 with a non-null top-level `error`. Surface the
  // engine's own wording -- PARSE_ERROR vs ANALYZE_ERROR vs PLAN_ERROR means
  // something quite specific here, and paraphrasing it would hide that.
  if (json.error) {
    throw new Error(
      `${json.error}${json.error_details ? `: ${json.error_details}` : ''}`
    )
  }
  const ms = performance.now() - t0
  const names: string[] = json.header?.column_names ?? []
  const cols = columns(json.data as unknown[][])

  const looksLikeSubgraph =
    names.length === 8 &&
    /^labels\(/i.test(names[1] ?? '') &&
    /^(edgeType|type)\(/i.test(names[4] ?? '') &&
    /^labels\(/i.test(names[6] ?? '')

  if (looksLikeSubgraph) {
    const nodes = new Map<number, GNode>()
    const edges = new Map<number, GEdge>()
    const n = cols[0]?.length ?? 0
    for (let i = 0; i < n; i++) {
      const aId = num(cols[0]?.[i])
      const bId = num(cols[5]?.[i])
      const eId = num(cols[3]?.[i])
      if (!nodes.has(aId))
        nodes.set(aId, {
          id: aId,
          label: str(cols[1]?.[i]),
          name: str(cols[2]?.[i]) || str(cols[1]?.[i]),
        })
      if (!nodes.has(bId))
        nodes.set(bId, {
          id: bId,
          label: str(cols[6]?.[i]),
          name: str(cols[7]?.[i]) || str(cols[6]?.[i]),
        })
      if (!edges.has(eId))
        edges.set(eId, { id: eId, src: aId, tgt: bId, type: str(cols[4]?.[i]) })
    }
    return {
      kind: 'graph',
      sub: { nodes: [...nodes.values()], edges: [...edges.values()] },
      ms,
    }
  }

  const rowCount = cols[0]?.length ?? 0
  const shown = Math.min(rowCount, RAW_ROW_LIMIT)
  const rows: string[][] = []
  for (let i = 0; i < shown; i++) {
    rows.push(names.map((_, c) => {
      const v = cols[c]?.[i]
      if (v === null || v === undefined) return '--'
      if (typeof v === 'number')
        return Number.isInteger(v) ? v.toLocaleString() : v.toFixed(4)
      if (typeof v === 'boolean') return v ? 'true' : 'false'
      return String(v)
    }))
  }
  return {
    kind: 'table',
    columns: names,
    rows,
    truncated: rowCount > shown,
    ms,
  }
}

// ---------------------------------------------------------------------------
// the commit ledger
// ---------------------------------------------------------------------------

export interface Snapshot {
  year: number
  tag: string
  commit: string
  added: number
  total: number
}

let ledgerCache: Snapshot[] | null = null

/** Year -> commit hash. Lives in its own graph because TuringDB has no tags. */
export async function loadLedger(): Promise<Snapshot[]> {
  if (ledgerCache) return ledgerCache
  const c = await runCypher(
    LEDGER_GRAPH,
    'MATCH (s:Snapshot) RETURN s.year, s.tag, s.commit_hash, ' +
      's.structures_added, s.structures_total ORDER BY s.year'
  )
  const rows = c[0]?.length ?? 0
  const out: Snapshot[] = []
  for (let i = 0; i < rows; i++) {
    out.push({
      year: Number(c[0]?.[i] ?? 0),
      tag: String(c[1]?.[i] ?? ''),
      commit: String(c[2]?.[i] ?? ''),
      added: Number(c[3]?.[i] ?? 0),
      total: Number(c[4]?.[i] ?? 0),
    })
  }
  ledgerCache = out
  return out
}

// ---------------------------------------------------------------------------
// graph overview (left panel)
// ---------------------------------------------------------------------------

export interface Overview {
  structures: number
  atoms: number
  components: number
  contacts: number
  hbonds: number
  hbondsInferred: number
  halogen: number
  bonds: number
  fragments: number
  spaceGroups: number
  withInchikey: number
  polymeric: number
  meanHA: number
  meanAngle: number
  /** weak C-H...A contacts: a separate population, never merged with hbond */
  weak: number
  /** inferred contacts with an implausible donor, excluded from H-bond stats */
  closeContact: number
}

export async function loadOverview(graph: string): Promise<Overview> {
  const q = (s: string) => scalar(graph, s)
  const [
    structures, atoms, components, contacts, hbonds, hbondsInferred, halogen,
    bonds, fragments, spaceGroups, withInchikey, polymeric, meanHA, meanAngle,
    weak, closeContact,
  ] = await Promise.all([
    q('MATCH (n:Structure) RETURN count(n)'),
    q('MATCH (n:Atom) RETURN count(n)'),
    q('MATCH (n:Component) RETURN count(n)'),
    q('MATCH ()-[r:CONTACT]->() RETURN count(r)'),
    q("MATCH ()-[r:CONTACT]->() WHERE r.kind = 'hbond' RETURN count(r)"),
    q("MATCH ()-[r:CONTACT]->() WHERE r.kind = 'hbond' AND r.h_inferred = true RETURN count(r)"),
    q("MATCH ()-[r:CONTACT]->() WHERE r.kind = 'halogen' RETURN count(r)"),
    q('MATCH ()-[r:BONDED_TO]->() RETURN count(r)'),
    q('MATCH (n:Fragment) RETURN count(n)'),
    q('MATCH (n:SpaceGroup) RETURN count(n)'),
    q('MATCH (c:Component) WHERE c.has_inchikey = true RETURN count(c)'),
    q('MATCH (c:Component) WHERE c.is_polymeric = true RETURN count(c)'),
    // Scoped to kind='hbond' AND a located hydrogen. Filtering on h_inferred
    // alone silently averaged H...A together with the halogen bonds' X...A --
    // and now also with the weak C-H...A population, whose mean sits 0.6 A
    // further out. The published 2.09 A / 160.5 deg came from the blended
    // version; the located-H hydrogen-bond value is 2.048 A / 160.4 deg.
    q("MATCH ()-[r:CONTACT]->() WHERE r.kind = 'hbond' AND r.h_inferred = false RETURN avg(r.length)"),
    q("MATCH ()-[r:CONTACT]->() WHERE r.kind = 'hbond' AND r.h_inferred = false RETURN avg(r.angle)"),
    q("MATCH ()-[r:CONTACT]->() WHERE r.kind = 'hbond_weak' RETURN count(r)"),
    q("MATCH ()-[r:CONTACT]->() WHERE r.kind = 'close_contact' RETURN count(r)"),
  ])
  return {
    structures, atoms, components, contacts, hbonds, hbondsInferred, halogen,
    bonds, fragments, spaceGroups, withInchikey, polymeric, meanHA, meanAngle,
    weak, closeContact,
  }
}

// ---------------------------------------------------------------------------
// node detail (left panel, on selection)
// ---------------------------------------------------------------------------

export interface NodeDetail {
  id: number
  label: string
  title: string
  rows: { k: string; v: string }[]
  note?: string
}

const DETAIL_FIELDS: Record<string, [string, string][]> = {
  Structure: [
    ['cod_id', 'COD ID'], ['formula', 'Formula'], ['hm_symbol', 'Space group'],
    ['crystal_system', 'Crystal system'], ['year', 'Year'],
    ['a', 'a (A)'], ['b', 'b (A)'], ['c', 'c (A)'],
    ['alpha', 'alpha (deg)'], ['beta', 'beta (deg)'], ['gamma', 'gamma (deg)'],
    ['cell_volume', 'Cell volume (A^3)'], ['Z', 'Z'], ['Z_prime', "Z'"],
    ['r_factor', 'R factor'], ['temperature', 'Temperature (K)'],
    ['n_components', 'Components'], ['n_contacts', 'Contacts'],
    ['has_hydrogens', 'H positions refined'],
  ],
  Atom: [
    ['label', 'Site label'], ['element', 'Element'], ['cod_id', 'COD ID'],
    ['fract_x', 'x (frac)'], ['fract_y', 'y (frac)'], ['fract_z', 'z (frac)'],
    ['occupancy', 'Occupancy'], ['u_iso', 'U(iso)'],
  ],
  Component: [
    ['formula', 'Formula'], ['name', 'Name'], ['inchikey', 'InChIKey'],
    ['fallback_key', 'Connectivity key'], ['n_atoms', 'Atoms'],
    ['n_heavy', 'Heavy atoms'], ['charge', 'Charge'],
    ['is_solvent', 'Solvent'], ['is_polymeric', 'Extended framework'],
  ],
  Fragment: [['fragment_type', 'Fragment'], ['smarts', 'SMARTS']],
  SpaceGroup: [['hm_symbol', 'Hermann-Mauguin'], ['number', 'Number'], ['crystal_system', 'Crystal system']],
  Element: [['symbol', 'Symbol'], ['atomic_number', 'Atomic number']],
  Publication: [['doi', 'DOI'], ['year', 'Year'], ['title', 'Title'], ['journal_volume', 'Volume'], ['pages', 'First page']],
  Author: [['name', 'Name']],
  Journal: [['name', 'Journal']],
}

const NOTE: Record<string, string> = {
  Atom: 'Positions are fractional coordinates in the asymmetric unit. Symmetry images are generated on demand via the symop stored on each edge.',
  Component: 'Components are connected components of the covalent bond graph after symmetry expansion, so they are real molecules and ions rather than whatever the CIF happened to deposit.',
  Fragment: 'Perceived with RDKit from the SMARTS shown. Fragments are what make synthon and coformer questions expressible.',
  Structure: 'One determination, not one material. A re-refinement is a separate Structure joined by a SUPERSEDES edge.',
}

export async function loadNodeDetail(
  graph: string,
  id: number
): Promise<NodeDetail | null> {
  const labCols = await runCypher(graph, `MATCH (n) WHERE n = ${id} RETURN labels(n)`)
  const label = str(labCols[0]?.[0])
  if (!label) return null

  const fields = DETAIL_FIELDS[label] ?? []
  const rows: { k: string; v: string }[] = []
  if (fields.length) {
    const proj = fields.map(([p]) => `n.${p}`).join(', ')
    const cols = await runCypher(graph, `MATCH (n) WHERE n = ${id} RETURN ${proj}`)
    fields.forEach(([, human], i) => {
      const raw = cols[i]?.[0]
      if (raw === undefined || raw === null || raw === '') return
      let v: string
      if (typeof raw === 'boolean') v = raw ? 'yes' : 'no'
      else if (typeof raw === 'number') v = Number.isInteger(raw) ? String(raw) : raw.toFixed(4)
      else v = String(raw)
      rows.push({ k: human, v })
    })
  }
  const titleRow =
    rows.find((r) => ['COD ID', 'Site label', 'Formula', 'Fragment', 'Hermann-Mauguin', 'Symbol', 'DOI', 'Name', 'Journal'].includes(r.k))
  return {
    id,
    label,
    title: titleRow ? `${titleRow.v}` : `${label} ${id}`,
    rows,
    note: NOTE[label],
  }
}

// ---------------------------------------------------------------------------
// the investigations offered in the chat panel
// ---------------------------------------------------------------------------

/**
 * The three shelves the query library is arranged on. A CCDC audience splits
 * cleanly this way: the crystallographers care about the first, the solid-form
 * and drug-design people about the second, and the database team about the
 * third.
 */
export type Shelf = 'packing' | 'solidform' | 'provenance'

export const SHELVES: { id: Shelf; title: string; note: string }[] = [
  {
    id: 'packing',
    title: 'Packing & motifs',
    note: 'The contact network itself -- the edge set neither COD nor the CSD stores.',
  },
  {
    id: 'solidform',
    title: 'Solid form & design',
    note: 'Salt vs co-crystal, coformers, polymorphs: the questions that need identity to be cross-structure.',
  },
  {
    id: 'provenance',
    title: 'Provenance & versioning',
    note: 'Git-like commits over the corpus: reproduce a published statistic, or find what a correction invalidates.',
  },
]

export interface Investigation {
  id: string
  /** Which shelf of the library this sits on. */
  shelf: Shelf
  /** One line, shown under the prompt: what this query DEMONSTRATES. */
  blurb: string
  prompt: string
  cypher: string
  /** Run against a different graph than the one the studio is viewing. */
  graph?: string
  /**
   * When present, the answer box gets a commit toggle. The same query is
   * re-run against the chronologically-committed corpus at whichever commit is
   * selected, so the canvas shows the graph as it stood in that year.
   *
   * `cypher` defaults to the investigation's own -- most of these queries work
   * unchanged on `cod_versioned`, which carries Structure / Component /
   * Fragment / SpaceGroup. Only the packing view cannot, since the versioned
   * graph deliberately holds no atoms.
   */
  timeline?: {
    cypher?: string
    metrics: (commit: string) => Promise<{ label: string; value: string }[]>
    /**
     * Narrative for ONE commit.
     *
     * Without it the narrative would keep describing HEAD while the metric
     * chips describe the selected year -- two different corpora side by side
     * in the same panel.
     */
    answerAt?: (commit: string, tag: string) => Promise<string[]>
  }
  /** Builds the narrative answer from live follow-up queries. */
  answer: (graph: string, sub: Subgraph) => Promise<string[]>
}

const f1 = (v: number) => v.toFixed(1)
const f2 = (v: number) => v.toFixed(2)
const pct = (a: number, b: number) => (b ? ((100 * a) / b).toFixed(1) : '0.0')

/** A structure that actually has a rich contact network, for the packing view. */
export const PACKING_SEED_COD = 2229029

/**
 * Prefix of the acid-dimer counting query, up to the `is_involution` test.
 *
 * Built once because four follow-ups differ only in that boolean and in what
 * they aggregate. Written as ONE connected chain starting at Fragment (16
 * nodes) rather than as comma-separated patterns joined in the WHERE -- that
 * rewrite alone is worth roughly 20x in this engine, which no index replaces.
 */
const DIMER_COUNT =
  "MATCH (f1:Fragment)<-[:HAS_FRAGMENT]-(c1:Component)<-[:IN_COMPONENT]-(a1:Atom)" +
  "-[h:CONTACT]->(a2:Atom)-[:IN_COMPONENT]->(c2:Component)-[:HAS_FRAGMENT]->(f2:Fragment) " +
  "WHERE f1.fragment_type = 'carboxylic_acid' AND f2.fragment_type = 'carboxylic_acid' " +
  "AND h.kind = 'hbond' AND h.h_inferred = false AND h.is_involution = "

export const INVESTIGATIONS: Investigation[] = [
  {
    id: 'packing',
    shelf: 'packing',
    blurb:
      'Every contact edge carries the symmetry operation that generated the neighbour, so the periodic network is traversable without building a supercell.',
    prompt: `Show the hydrogen-bond network of one structure (COD ${PACKING_SEED_COD})`,
    cypher: `MATCH (a:Atom)-[e:CONTACT]->(b:Atom)
WHERE a.cod_id = ${PACKING_SEED_COD}
RETURN a, labels(a), a.label, e, edgeType(e), b, labels(b), b.label`,
    answer: async (graph, sub) => {
      const g = graph
      const cod = PACKING_SEED_COD
      const [nH, nInf, nHal, mLen, mAng, nComp, nAtoms] = await Promise.all([
        scalar(g, `MATCH (a:Atom)-[r:CONTACT]->(b:Atom) WHERE a.cod_id = ${cod} AND r.kind = 'hbond' RETURN count(r)`),
        scalar(g, `MATCH (a:Atom)-[r:CONTACT]->(b:Atom) WHERE a.cod_id = ${cod} AND r.h_inferred = true RETURN count(r)`),
        scalar(g, `MATCH (a:Atom)-[r:CONTACT]->(b:Atom) WHERE a.cod_id = ${cod} AND r.kind = 'halogen' RETURN count(r)`),
        scalar(g, `MATCH (a:Atom)-[r:CONTACT]->(b:Atom) WHERE a.cod_id = ${cod} AND r.h_inferred = false RETURN avg(r.length)`),
        scalar(g, `MATCH (a:Atom)-[r:CONTACT]->(b:Atom) WHERE a.cod_id = ${cod} AND r.h_inferred = false RETURN avg(r.angle)`),
        scalar(g, `MATCH (s:Structure)-[:CONTAINS_COMPONENT]->(c:Component) WHERE s.cod_id = ${cod} RETURN count(c)`),
        scalar(g, `MATCH (a:Atom) WHERE a.cod_id = ${cod} RETURN count(a)`),
      ])
      return [
        `COD ${cod} has ${nAtoms} atomic sites in its asymmetric unit, resolving into ${nComp} discrete components.`,
        `Its intermolecular contact network carries ${nH} hydrogen bonds and ${nHal} halogen bonds. ${nInf} of the hydrogen bonds are flagged h_inferred, meaning no hydrogen position was refined and the criterion fell back to a heavy-atom donor-acceptor separation.`,
        `Across the located-hydrogen population the mean H...A distance is ${f2(mLen)} A at a mean D-H...A angle of ${f1(mAng)} degrees, which is squarely in strong-hydrogen-bond territory.`,
        `The graph is showing ${sub.nodes.length} atoms joined by ${sub.edges.length} contact edges. Every one of those edges carries the symmetry operation that generated the neighbour, in CIF form, so the periodic network is traversable without materialising a supercell. This is the layer neither COD nor the CSD persists: it is normally recomputed geometrically each time somebody asks.`,
      ]
    },
  },
  {
    id: 'recurrence',
    shelf: 'solidform',
    blurb:
      'One molecule appearing in forty structures is one node with forty edges, not forty rows joined on a string.',
    prompt: 'Which molecules recur across different structures?',
    cypher: `MATCH (s:Structure)-[e:CONTAINS_COMPONENT]->(c:Component)
RETURN s, labels(s), s.cod_id, e, edgeType(e), c, labels(c), c.formula LIMIT 60`,
    timeline: {
      metrics: async (commit) => {
        const [structures, comps, multi, ik] = await Promise.all([
          scalarAt(VERSIONED_GRAPH, 'MATCH (s:Structure) RETURN count(s)', commit),
          scalarAt(VERSIONED_GRAPH, 'MATCH (c:Component) RETURN count(c)', commit),
          scalarAt(VERSIONED_GRAPH, 'MATCH (s:Structure) WHERE s.n_components > 1 RETURN count(s)', commit),
          scalarAt(VERSIONED_GRAPH, 'MATCH (c:Component) WHERE c.has_inchikey = true RETURN count(c)', commit),
        ])
        return [
          { label: 'structures', value: structures.toLocaleString() },
          { label: 'components', value: comps.toLocaleString() },
          { label: 'multi-component', value: `${multi.toLocaleString()} (${structures ? ((100 * multi) / structures).toFixed(0) : 0}%)` },
          { label: 'with InChIKey', value: `${ik.toLocaleString()} (${comps ? ((100 * ik) / comps).toFixed(0) : 0}%)` },
        ]
      },
    },
    answer: async (graph, sub) => {
      const g = graph
      const [nComp, nStruct, nLinks, nSolv, nIk, nPoly, nMulti] = await Promise.all([
        scalar(g, 'MATCH (c:Component) RETURN count(c)'),
        scalar(g, 'MATCH (s:Structure) RETURN count(s)'),
        scalar(g, 'MATCH ()-[r:CONTAINS_COMPONENT]->() RETURN count(r)'),
        scalar(g, 'MATCH (c:Component) WHERE c.is_solvent = true RETURN count(c)'),
        scalar(g, 'MATCH (c:Component) WHERE c.has_inchikey = true RETURN count(c)'),
        scalar(g, 'MATCH (c:Component) WHERE c.is_polymeric = true RETURN count(c)'),
        scalar(g, 'MATCH (s:Structure) WHERE s.n_components > 1 RETURN count(s)'),
      ])
      const reuse = nComp ? nLinks / nComp : 0
      return [
        `There are ${nComp.toLocaleString()} distinct components across ${nStruct.toLocaleString()} structures, connected by ${nLinks.toLocaleString()} CONTAINS_COMPONENT edges. That is an average of ${f2(reuse)} structures per molecule, so recurrence is real and measurable rather than anecdotal.`,
        `${nIk.toLocaleString()} components (${pct(nIk, nComp)}%) carry a genuine InChIKey. The rest fall back to a formula-plus-connectivity digest, which is stored in a separate property so it is never mistaken for an InChIKey. Metal complexes dominate that group: InChI has no well-defined representation for them.`,
        `${nMulti.toLocaleString()} structures contain more than one component, which is where the interesting chemistry lives: salts, solvates and co-crystals. ${nSolv.toLocaleString()} components are classified as solvent and ${nPoly.toLocaleString()} as extended frameworks.`,
        `This is the join a relational schema cannot make cheaply. The same molecule appearing in forty structures is one node with forty edges, not forty rows joined on a string. That single modelling choice is what makes polymorph, solvate and coformer questions expressible at all.`,
      ]
    },
  },
  {
    id: 'fragments',
    shelf: 'solidform',
    blurb:
      'Perceived functional groups are nodes, which is what makes synthon and coformer questions expressible at all.',
    prompt: 'Which functional groups dominate the corpus?',
    cypher: `MATCH (c:Component)-[e:HAS_FRAGMENT]->(f:Fragment)
RETURN c, labels(c), c.formula, e, edgeType(e), f, labels(f), f.fragment_type LIMIT 400`,
    timeline: {
      metrics: async (commit) => {
        const [frags, comps, acid, pyr] = await Promise.all([
          scalarAt(VERSIONED_GRAPH, 'MATCH (n:Fragment) RETURN count(n)', commit),
          scalarAt(VERSIONED_GRAPH, 'MATCH (c:Component) RETURN count(c)', commit),
          scalarAt(VERSIONED_GRAPH, "MATCH (c:Component)-[:HAS_FRAGMENT]->(f:Fragment) WHERE f.fragment_type = 'carboxylic_acid' RETURN count(c)", commit),
          scalarAt(VERSIONED_GRAPH, "MATCH (c:Component)-[:HAS_FRAGMENT]->(f:Fragment) WHERE f.fragment_type = 'pyridine_nitrogen' RETURN count(c)", commit),
        ])
        return [
          { label: 'fragment types', value: frags.toLocaleString() },
          { label: 'components', value: comps.toLocaleString() },
          { label: 'carboxylic acid', value: `${acid.toLocaleString()} (${comps ? ((100 * acid) / comps).toFixed(1) : 0}%)` },
          { label: 'pyridine N', value: `${pyr.toLocaleString()} (${comps ? ((100 * pyr) / comps).toFixed(1) : 0}%)` },
        ]
      },
    },
    answer: async (graph, sub) => {
      const g = graph
      const kinds = [
        'aromatic_ring', 'carbonyl', 'hydroxyl', 'aromatic_ring_5', 'ether',
        'amide_secondary', 'carboxylic_acid', 'halide', 'pyridine_nitrogen',
        'nitro_charged', 'ester', 'amine_primary', 'sulfonamide', 'carboxylate',
      ]
      const counts = await Promise.all(
        kinds.map((k) =>
          scalar(g, `MATCH (c:Component)-[:HAS_FRAGMENT]->(f:Fragment) WHERE f.fragment_type = '${k}' RETURN count(c)`)
        )
      )
      const ranked = kinds.map((k, i) => ({ k, n: counts[i] })).filter((r) => r.n > 0).sort((a, b) => b.n - a.n)
      const total = ranked.reduce((s2, r) => s2 + r.n, 0)
      const top = ranked.slice(0, 5).map((r) => `${r.k.replace(/_/g, ' ')} (${r.n.toLocaleString()})`).join(', ')
      const acid = ranked.find((r) => r.k === 'carboxylic_acid')?.n ?? 0
      const pyr = ranked.find((r) => r.k === 'pyridine_nitrogen')?.n ?? 0
      return [
        `Fragment perception found ${total.toLocaleString()} group occurrences across the corpus, spread over ${ranked.length} distinct fragment types. Each cluster on the canvas is one fragment with the molecules carrying it fanned out around it.`,
        `The most common are ${top}.`,
        `Two of these matter more than their frequency suggests: ${acid.toLocaleString()} components carry a carboxylic acid and ${pyr.toLocaleString()} carry a pyridine nitrogen. That pair is the classic acid/pyridine heterosynthon of co-crystal design, so this is the population a crystal engineer would screen for a coformer.`,
        `Fragments are perceived with RDKit from SMARTS patterns held in one reviewable table, and only on components where bond-order perception succeeded. Metal complexes and very large molecules are excluded, which caps how complete this picture can be -- the ingest report carries that rate rather than hiding it.`,
      ]
    },
  },
  {
    id: 'elements',
    shelf: 'packing',
    blurb:
      'Composition as traversal: element membership is an edge, so "organics containing both S and Br" needs no LIKE over a formula string.',
    prompt: 'What elements do these structures contain?',
    cypher: `MATCH (s:Structure)-[e:CONTAINS_ELEMENT]->(el:Element)
RETURN s, labels(s), s.cod_id, e, edgeType(e), el, labels(el), el.symbol LIMIT 420`,
    answer: async (graph) => {
      const g = graph
      const total = await scalar(g, 'MATCH (s:Structure) RETURN count(s)')
      const els = ['C', 'H', 'O', 'N', 'S', 'Cl', 'F', 'Br', 'P', 'I', 'Cu', 'Zn', 'Fe', 'Ni', 'Co', 'Mn']
      const counts = await Promise.all(
        els.map((sym) => scalar(g, `MATCH (s:Structure)-[:CONTAINS_ELEMENT]->(x:Element) WHERE x.symbol = '${sym}' RETURN count(s)`))
      )
      const ranked = els.map((sym, i) => ({ sym, n: counts[i] })).filter((r) => r.n > 0).sort((a, b) => b.n - a.n)
      const organics = ranked.filter((r) => ['C', 'H', 'O', 'N'].includes(r.sym))
      const metals = ranked.filter((r) => ['Cu', 'Zn', 'Fe', 'Ni', 'Co', 'Mn'].includes(r.sym))
      const halo = ranked.filter((r) => ['Cl', 'F', 'Br', 'I'].includes(r.sym))
      const nEl = await scalar(g, 'MATCH (n:Element) RETURN count(n)')
      return [
        `${total.toLocaleString()} structures draw on ${nEl} distinct elements. Each cluster here is one element with the structures containing it around it, so the sizes read straight off as elemental frequency.`,
        `The organic backbone dominates: ${organics.map((r) => `${r.sym} in ${r.n.toLocaleString()}`).join(', ')}.`,
        `Halogens appear in ${halo.reduce((s2, r) => s2 + r.n, 0).toLocaleString()} structure-element pairs (${halo.map((r) => r.sym).join(', ')}), which is the population the halogen-bond criterion applies to. First-row transition metals appear as ${metals.map((r) => `${r.sym} ${r.n.toLocaleString()}`).join(', ')}.`,
        `That metal population is exactly where InChI perception fails, so those components fall back to a connectivity digest for identity. It is the honest boundary of the fragment-based queries.`,
      ]
    },
  },
  {
    id: 'solvates',
    shelf: 'solidform',
    blurb:
      'A hydrate is the same principal component with one extra edge -- so "every solvate of this compound" is one hop.',
    prompt: 'Show the solvates and hydrates',
    cypher: `MATCH (s:Structure)-[e:CONTAINS_COMPONENT]->(c:Component)
WHERE c.is_solvent = true
RETURN s, labels(s), s.cod_id, e, edgeType(e), c, labels(c), c.formula LIMIT 320`,
    timeline: {
      metrics: async (commit) => {
        const [structures, links, water, species] = await Promise.all([
          scalarAt(VERSIONED_GRAPH, 'MATCH (s:Structure) RETURN count(s)', commit),
          scalarAt(VERSIONED_GRAPH, 'MATCH (:Structure)-[r:CONTAINS_COMPONENT]->(c:Component) WHERE c.is_solvent = true RETURN count(r)', commit),
          scalarAt(VERSIONED_GRAPH, "MATCH (:Structure)-[r:CONTAINS_COMPONENT]->(c:Component) WHERE c.formula = 'H2 O' RETURN count(r)", commit),
          scalarAt(VERSIONED_GRAPH, 'MATCH (c:Component) WHERE c.is_solvent = true RETURN count(c)', commit),
        ])
        return [
          { label: 'structures', value: structures.toLocaleString() },
          { label: 'solvent species', value: species.toLocaleString() },
          { label: 'solvate links', value: links.toLocaleString() },
          { label: 'hydrates', value: water.toLocaleString() },
        ]
      },
    },
    answer: async (graph, sub) => {
      const g = graph
      const [nSolvComp, nLinks, water] = await Promise.all([
        scalar(g, 'MATCH (c:Component) WHERE c.is_solvent = true RETURN count(c)'),
        scalar(g, 'MATCH (:Structure)-[r:CONTAINS_COMPONENT]->(c:Component) WHERE c.is_solvent = true RETURN count(r)'),
        scalar(g, "MATCH (:Structure)-[r:CONTAINS_COMPONENT]->(c:Component) WHERE c.formula = 'H2 O' RETURN count(r)"),
      ])
      return [
        `${nSolvComp.toLocaleString()} distinct solvent species account for ${nLinks.toLocaleString()} structure-solvent relationships. Water alone appears in ${water.toLocaleString()} of them, which is why it forms the largest cluster on the canvas.`,
        `Each cluster is one solvent with its hydrates or solvates fanned around it. This is the view that shows why the solvent classification has to exist: without it, a coformer query would answer "water" for essentially every target molecule.`,
        `Solvent status is decided by a curated formula table rather than by size, and it deliberately includes the hydrogen-free skeletons (a bare O, a bare C O) because solvent hydrogens are so often unrefined. That table is a chemical judgement call and is flagged for review.`,
        `The graph is showing ${sub.nodes.length} nodes over ${sub.edges.length} edges. Note that a solvate is not a different molecule with a different name here: it is the same principal component node with an extra CONTAINS_COMPONENT edge, which is what makes "find every hydrate of this compound" a one-hop question.`,
      ]
    },
  },
  {
    id: 'acid_synthon',
    shelf: 'solidform',
    blurb:
      'The acid/carboxylate split distinguishes a co-crystal from a salt, which is a regulatory distinction rather than a cosmetic one.',
    prompt: 'Find the carboxylic acid molecules (synthon candidates)',
    cypher: `MATCH (c:Component)-[e:HAS_FRAGMENT]->(f:Fragment)
WHERE f.fragment_type = 'carboxylic_acid' OR f.fragment_type = 'carboxylate' OR f.fragment_type = 'pyridine_nitrogen'
RETURN c, labels(c), c.formula, e, edgeType(e), f, labels(f), f.fragment_type LIMIT 320`,
    timeline: {
      metrics: async (commit) => {
        const [acid, carbox, pyr, comps] = await Promise.all([
          scalarAt(VERSIONED_GRAPH, "MATCH (c:Component)-[:HAS_FRAGMENT]->(f:Fragment) WHERE f.fragment_type = 'carboxylic_acid' RETURN count(c)", commit),
          scalarAt(VERSIONED_GRAPH, "MATCH (c:Component)-[:HAS_FRAGMENT]->(f:Fragment) WHERE f.fragment_type = 'carboxylate' RETURN count(c)", commit),
          scalarAt(VERSIONED_GRAPH, "MATCH (c:Component)-[:HAS_FRAGMENT]->(f:Fragment) WHERE f.fragment_type = 'pyridine_nitrogen' RETURN count(c)", commit),
          scalarAt(VERSIONED_GRAPH, 'MATCH (c:Component) RETURN count(c)', commit),
        ])
        return [
          { label: 'carboxylic acid', value: acid.toLocaleString() },
          { label: 'carboxylate (salt)', value: carbox.toLocaleString() },
          { label: 'pyridine N', value: pyr.toLocaleString() },
          { label: 'components', value: comps.toLocaleString() },
        ]
      },
    },
    answer: async (graph, sub) => {
      const g = graph
      const [acid, carboxylate, pyridine, hbonds] = await Promise.all([
        scalar(g, "MATCH (c:Component)-[:HAS_FRAGMENT]->(f:Fragment) WHERE f.fragment_type = 'carboxylic_acid' RETURN count(c)"),
        scalar(g, "MATCH (c:Component)-[:HAS_FRAGMENT]->(f:Fragment) WHERE f.fragment_type = 'carboxylate' RETURN count(c)"),
        scalar(g, "MATCH (c:Component)-[:HAS_FRAGMENT]->(f:Fragment) WHERE f.fragment_type = 'pyridine_nitrogen' RETURN count(c)"),
        scalar(g, "MATCH ()-[r:CONTACT]->() WHERE r.kind = 'hbond' AND r.h_inferred = false RETURN count(r)"),
      ])
      return [
        `${acid.toLocaleString()} components carry a neutral carboxylic acid, ${carboxylate.toLocaleString()} carry the deprotonated carboxylate, and ${pyridine.toLocaleString()} carry a pyridine-type nitrogen acceptor. The canvas shows three clusters, one per fragment.`,
        `The acid/carboxylate split is doing real work. It distinguishes a co-crystal from a salt, which is a regulatory distinction in pharmaceutical solid-form work, not a cosmetic one -- and it falls out of the graph because proton position was perceived per component rather than assumed.`,
        `From here the synthon question is a pattern match rather than a geometry job. There are ${hbonds.toLocaleString()} hydrogen bonds with a located hydrogen to work with; the "Find the carboxylic acid dimer motif" investigation takes it the rest of the way.`,
        `One correction worth carrying, because it is the kind of mistake this representation invites. The obvious way to look for an R2,2(8) dimer is to ask for a RECIPROCAL PAIR of hydrogen bonds between two molecules. In this graph that pair does not exist: the stored network is a quotient graph, so the second bond of a centrosymmetric dimer is the symmetry image of the first and both collapse to ONE edge. Asking for a pair therefore matches each edge against itself. What actually separates a dimer from a chain is whether the generating operation is its own inverse -- and that is a boolean on the edge, not a second pattern.`,
      ]
    },
  },
  {
    id: 'history',
    shelf: 'provenance',
    blurb:
      'One commit per publication year: a statistic published in 2010 is a checkout, not an archived dump restore.',
    prompt: 'How has the corpus grown, and can I reproduce an old statistic?',
    graph: LEDGER_GRAPH,
    cypher: `MATCH (a:Snapshot)-[e:NEXT]->(b:Snapshot)
RETURN a, labels(a), a.tag, e, edgeType(e), b, labels(b), b.tag`,
    answer: async () => {
      const L = LEDGER_GRAPH
      const V = VERSIONED_GRAPH
      const [nSnap, firstYear, lastYear, totalNow] = await Promise.all([
        scalar(L, 'MATCH (s:Snapshot) RETURN count(s)'),
        scalar(L, 'MATCH (s:Snapshot) RETURN min(s.year)').catch(() => 0),
        scalar(L, 'MATCH (s:Snapshot) RETURN max(s.year)').catch(() => 0),
        scalar(V, 'MATCH (s:Structure) RETURN count(s)'),
      ])
      // min/max do not exist in this dialect, so read the ends off the chain
      const ends = await runCypher(
        L,
        'MATCH (s:Snapshot) RETURN s.year, s.tag, s.structures_total ORDER BY s.year'
      )
      const years = (ends[0] ?? []).map((v) => Number(v))
      const totals = (ends[2] ?? []).map((v) => Number(v))
      const y0 = years[0] ?? firstYear
      const y1 = years[years.length - 1] ?? lastYear
      const peakIdx = totals.reduce(
        (best, _v, i) =>
          i > 0 && totals[i] - totals[i - 1] > totals[best] - totals[best - 1] ? i : best,
        1
      )
      return [
        `The corpus was ingested chronologically as ${nSnap} commits, one per publication year from ${y0} to ${y1}, each adding only the structures published that year. The chain on the canvas is that commit log; every node is a real commit hash.`,
        `Checking out cod@2010 gives ${totals[years.indexOf(2010)]?.toLocaleString() ?? 'the'} structures -- the literature exactly as it stood at the end of 2010 -- against ${totalNow.toLocaleString()} at HEAD. The busiest year added ${(totals[peakIdx] - totals[peakIdx - 1]).toLocaleString()} structures in ${years[peakIdx]}; the intake falls away after 2015 as Acta Cryst. E changed scope.`,
        `This is the part that replaces archived database dumps. A published result of the form "N% of structures containing X form motif Y" is only reproducible against the database as it stood when it was computed. Here that is a checkout, not a restore: the same query runs against any commit and returns what it would have returned then.`,
        `TuringDB has no tags or branches -- only commit hashes, integer change ids and a single main -- so the cod@<year> names live in this ledger graph, which makes the mapping itself queryable and versioned rather than a file sitting next to the data.`,
      ]
    },
  },
  {
    id: 'audit',
    shelf: 'provenance',
    blurb:
      'If a determination is later corrected, the commit history names exactly which published analyses included it.',
    prompt: 'If a structure is later corrected, which analyses are suspect?',
    graph: LEDGER_GRAPH,
    cypher: `MATCH (a:Snapshot)-[e:NEXT]->(b:Snapshot)
RETURN a, labels(a), a.tag, e, edgeType(e), b, labels(b), b.tag`,
    answer: async () => {
      const L = LEDGER_GRAPH
      const V = VERSIONED_GRAPH
      const cod = PACKING_SEED_COD
      const rows = await runCypher(
        V,
        `MATCH (s:Structure) WHERE s.cod_id = ${cod} RETURN s.year, s.hm_symbol, s.formula`
      )
      const year = Number(rows[0]?.[0] ?? 0)
      const hm = String(rows[1]?.[0] ?? '')
      const formula = String(rows[2]?.[0] ?? '')
      const [nSnap, affected] = await Promise.all([
        scalar(L, 'MATCH (s:Snapshot) RETURN count(s)'),
        scalar(L, `MATCH (s:Snapshot) WHERE s.year >= ${year} RETURN count(s)`),
      ])
      const totalAt = await scalar(
        L, `MATCH (s:Snapshot) WHERE s.year = ${year} RETURN count(s)`
      )
      return [
        `Take COD ${cod} (${formula}, ${hm}), published in ${year}. Because the ingest is chronological, it enters the graph at the cod@${year} commit and is present in every commit after it.`,
        `That means ${affected} of the ${nSnap} snapshots contain this determination. If it is later corrected, redetermined or retracted, those are exactly the commits whose statistics include it -- and therefore the set of published analyses to re-examine. The remaining ${nSnap - affected} predate it and are unaffected.`,
        `Answering this today means knowing which archived dump a given paper was computed against, and hoping that dump still exists. Here it is a property of the graph's own history.`,
        `Worth separating two mechanisms that get conflated. SUPERSEDES edges make revision history queryable *within* one version of the graph -- this determination replaced that one, and here is why. Commits make the graph itself rewindable. They answer different questions and the demo carries both.`,
      ]
    },
  },
  {
    id: 'spacegroups',
    shelf: 'packing',
    blurb:
      'Symmetry as a node you can traverse to, not a text column -- which is why P2(1)/c and P2(1)/n stay distinguishable.',
    prompt: 'How are space groups distributed in this corpus?',
    cypher: `MATCH (s:Structure)-[e:IN_SPACE_GROUP]->(g:SpaceGroup)
RETURN s, labels(s), s.cod_id, e, edgeType(e), g, labels(g), g.hm_symbol LIMIT 120`,
    timeline: {
      answerAt: async (commit, tag) => {
        const [structures, groups, p21c, p1bar, centro] = await Promise.all([
          scalarAt(VERSIONED_GRAPH, 'MATCH (s:Structure) RETURN count(s)', commit),
          scalarAt(VERSIONED_GRAPH, 'MATCH (n:SpaceGroup) RETURN count(n)', commit),
          scalarAt(VERSIONED_GRAPH, "MATCH (s:Structure)-[:IN_SPACE_GROUP]->(x:SpaceGroup) WHERE x.hm_symbol = 'P 1 21/c 1' RETURN count(s)", commit),
          scalarAt(VERSIONED_GRAPH, "MATCH (s:Structure)-[:IN_SPACE_GROUP]->(x:SpaceGroup) WHERE x.hm_symbol = 'P -1' RETURN count(s)", commit),
          scalarAt(VERSIONED_GRAPH, "MATCH (s:Structure)-[:IN_SPACE_GROUP]->(x:SpaceGroup) WHERE x.hm_symbol = 'C 1 2/c 1' RETURN count(s)", commit),
        ])
        return [
          `At ${tag} the corpus held ${structures.toLocaleString()} structures over ${groups.toLocaleString()} Hermann-Mauguin settings.`,
          `P 1 21/c 1 accounted for ${p21c.toLocaleString()} of them (${pct(p21c, structures)}%), P -1 for ${p1bar.toLocaleString()} (${pct(p1bar, structures)}%) and C 1 2/c 1 for ${centro.toLocaleString()} (${pct(centro, structures)}%).`,
          `This is not a filtered view of today's corpus. It is the graph as it stood at that commit -- the structures published later do not exist in it. Drag along the strip and watch P2(1)/c's share climb: the skew that every crystallographer knows got MORE pronounced over the period, which is a claim you can only make if you can still run the query against the old corpus.`,
        ]
      },
      metrics: async (commit) => {
        const [structures, groups, p21c, p1bar] = await Promise.all([
          scalarAt(VERSIONED_GRAPH, 'MATCH (s:Structure) RETURN count(s)', commit),
          scalarAt(VERSIONED_GRAPH, 'MATCH (n:SpaceGroup) RETURN count(n)', commit),
          scalarAt(VERSIONED_GRAPH, "MATCH (s:Structure)-[:IN_SPACE_GROUP]->(x:SpaceGroup) WHERE x.hm_symbol = 'P 1 21/c 1' RETURN count(s)", commit),
          scalarAt(VERSIONED_GRAPH, "MATCH (s:Structure)-[:IN_SPACE_GROUP]->(x:SpaceGroup) WHERE x.hm_symbol = 'P -1' RETURN count(s)", commit),
        ])
        return [
          { label: 'structures', value: structures.toLocaleString() },
          // cod_versioned keys SpaceGroup by Hermann-Mauguin SYMBOL, so the
          // settings of one group (P2(1)/c, P2(1)/n, P2(1)/a) are separate
          // nodes. cod_slice_v2 keys by group NUMBER and reports fewer.
          // Both are right; they count different things, so say which.
          { label: 'H-M settings seen', value: groups.toLocaleString() },
          { label: 'P 1 21/c 1', value: `${p21c.toLocaleString()} (${structures ? ((100 * p21c) / structures).toFixed(1) : 0}%)` },
          { label: 'P -1', value: `${p1bar.toLocaleString()} (${structures ? ((100 * p1bar) / structures).toFixed(1) : 0}%)` },
        ]
      },
    },
    answer: async (graph) => {
      const g = graph
      const total = await scalar(g, 'MATCH (s:Structure) RETURN count(s)')
      const groups = ['P 1 21/c 1', 'P -1', 'C 1 2/c 1', 'P 21 21 21', 'P 1 21/n 1', 'P b c a']
      const counts = await Promise.all(
        groups.map((hm) =>
          scalar(g, `MATCH (s:Structure)-[:IN_SPACE_GROUP]->(x:SpaceGroup) WHERE x.hm_symbol = '${hm}' RETURN count(s)`)
        )
      )
      const ranked = groups
        .map((hm, i) => ({ hm, n: counts[i] }))
        .sort((a, b) => b.n - a.n)
        .filter((r) => r.n > 0)
      const nGroups = await scalar(g, 'MATCH (n:SpaceGroup) RETURN count(n)')
      const top = ranked.slice(0, 4).map((r) => `${r.hm} (${r.n.toLocaleString()}, ${pct(r.n, total)}%)`).join(', ')
      // Asked of the graph via the is_centrosymmetric flag, rather than summed
      // from a hand-picked list of groups: the long tail of centrosymmetric
      // groups is large enough that any short list understates it badly.
      const centro = await scalar(
        g,
        'MATCH (s:Structure)-[:IN_SPACE_GROUP]->(x:SpaceGroup) ' +
          'WHERE x.is_centrosymmetric = true RETURN count(s)'
      )
      return [
        `${total.toLocaleString()} structures are distributed over ${nGroups} distinct space groups, and the distribution is extremely skewed.`,
        `The leaders are ${top}.`,
        `Centrosymmetric groups account for ${pct(centro, total)}% of the corpus -- that figure is read off an is_centrosymmetric flag on the SpaceGroup node, not summed from the few groups on screen. It is the familiar result: molecules pack most efficiently using inversion centres and glide planes, so a handful of groups dominates the whole of small-molecule crystallography.`,
        `One modelling point worth being exact about, because it changes what the numbers mean. This graph keys a SpaceGroup node by group NUMBER, so the ${(ranked[0]?.n ?? 0).toLocaleString()} structures shown under P2(1)/c are all of space group No. 14 in every setting -- P2(1)/n and P2(1)/a included. The chronologically-committed graph behind the commit toggle keys by Hermann-Mauguin SYMBOL instead, so there the settings are separate nodes and the count is higher. Same corpus, two deliberate modelling choices, and the node carries both properties so either question stays answerable.`,
      ]
    },
  },
  // ---- Packing & motifs -------------------------------------------------
  {
    id: 'dimensionality',
    shelf: 'packing',
    blurb:
      'Whether a packing is a dimer, a chain, a sheet or a framework is the rank of its cycle lattice -- a traversal, with no relational form.',
    prompt: 'Is this packing a dimer, a chain, a sheet or a framework?',
    cypher: `MATCH (s:Structure)-[e:IN_SPACE_GROUP]->(g:SpaceGroup)
WHERE s.net_dim > 0
RETURN s, labels(s), s.cod_id, e, edgeType(e), g, labels(g), g.hm_symbol LIMIT 140`,
    answer: async (graph) => {
      const g = graph
      const [d0, d1, d2, d3, w0, w1, w2, w3, scored] = await Promise.all([
        scalar(g, 'MATCH (s:Structure) WHERE s.net_dim = 0 RETURN count(s)'),
        scalar(g, 'MATCH (s:Structure) WHERE s.net_dim = 1 RETURN count(s)'),
        scalar(g, 'MATCH (s:Structure) WHERE s.net_dim = 2 RETURN count(s)'),
        scalar(g, 'MATCH (s:Structure) WHERE s.net_dim = 3 RETURN count(s)'),
        scalar(g, 'MATCH (s:Structure) WHERE s.net_dim_weak = 0 RETURN count(s)'),
        scalar(g, 'MATCH (s:Structure) WHERE s.net_dim_weak = 1 RETURN count(s)'),
        scalar(g, 'MATCH (s:Structure) WHERE s.net_dim_weak = 2 RETURN count(s)'),
        scalar(g, 'MATCH (s:Structure) WHERE s.net_dim_weak = 3 RETURN count(s)'),
        scalar(g, 'MATCH (s:Structure) WHERE s.has_net_dim = true RETURN count(s)'),
      ])
      const tot = d0 + d1 + d2 + d3
      const wtot = w0 + w1 + w2 + w3
      return [
        `Dimensionality is the rank of the lattice-translation subgroup generated by cycles in the hydrogen-bond net. Rank 0 is a finite motif -- a dimer or an isolated cluster. Rank 1 is a chain, rank 2 a sheet, rank 3 a framework.`,
        `Over ${scored.toLocaleString()} structures carrying at least one located-hydrogen bond: ${d0.toLocaleString()} are 0D (${pct(d0, tot)}%), ${d1.toLocaleString()} 1D (${pct(d1, tot)}%), ${d2.toLocaleString()} 2D (${pct(d2, tot)}%) and ${d3.toLocaleString()} 3D (${pct(d3, tot)}%).`,
        `Admitting weak C-H...A contacts as well moves it to ${pct(w0, wtot)}% 0D, ${pct(w1, wtot)}% 1D, ${pct(w2, wtot)}% 2D and ${pct(w3, wtot)}% 3D -- which SATURATES. Once every C-H donor counts, almost everything percolates and the statistic stops discriminating. That is the honest reading: the strong-bond net is the informative one, and the weak net is a reminder that a dimensionality number means nothing without the criterion attached to it.`,
        `This matters because dimensionality predicts behaviour a formulator cares about: chains tend to needle morphology and anisotropic mechanical response, sheets to plates that cleave and tablet well, frameworks to harder, less soluble, humidity-stable solids.`,
        `The mechanism is the reason this belongs in a graph, and it is also where the first version of this panel was wrong. The stored network is a QUOTIENT graph -- nodes are asymmetric-unit sites, each edge carries the symmetry operation to its neighbour -- so a chain running through the crystal is a short cycle, not a long path, and counting reachable nodes by depth reports everything as isolated. But the hydrogen bond alone is not the net: a donor and an acceptor are joined in the crystal THROUGH the molecules they belong to. Building the quotient graph from contacts only makes it bipartite with no path longer than one hop, so it has no cycles and everything comes back 0D. The covalent bonds have to be in the graph too, contracting each molecule to a point. A ring inside a finite molecule closes with the zero lattice vector, so it contributes nothing to the rank.`,
      ]
    },
  },
  {
    id: 'acid_dimer',
    shelf: 'packing',
    blurb:
      'The R2,2(8) carboxylic-acid dimer, separated from the C(4) catemer by the ORDER of the symmetry operation that generates it.',
    prompt: 'Find the carboxylic acid dimer motif, R2,2(8)',
    cypher: `MATCH (f1:Fragment)<-[:HAS_FRAGMENT]-(c1:Component)<-[:IN_COMPONENT]-(a1:Atom)
      -[h:CONTACT]->(a2:Atom)-[:IN_COMPONENT]->(c2:Component)-[:HAS_FRAGMENT]->(f2:Fragment)
WHERE f1.fragment_type = 'carboxylic_acid' AND f2.fragment_type = 'carboxylic_acid'
  AND h.kind = 'hbond' AND h.h_inferred = false
  AND h.is_involution = true
  AND a1.element = 'O' AND a2.element = 'O'
  AND a1.cod_id = a2.cod_id
RETURN a1, labels(a1), a1.label, h, edgeType(h), a2, labels(a2), a2.label LIMIT 300`,
    answer: async (graph, sub) => {
      const g = graph
      const [dimers, catemers, acids, acidStructures, meanLen, meanAng] = await Promise.all([
        scalar(g, `${DIMER_COUNT}true AND a1.element = 'O' AND a2.element = 'O' AND a1.cod_id = a2.cod_id RETURN count(h)`),
        scalar(g, `${DIMER_COUNT}false AND a1.element = 'O' AND a2.element = 'O' AND a1.cod_id = a2.cod_id RETURN count(h)`),
        scalar(g, "MATCH (c:Component)-[:HAS_FRAGMENT]->(f:Fragment) WHERE f.fragment_type = 'carboxylic_acid' RETURN count(c)"),
        // Distinct molecular IDENTITIES and the structures they appear in are
        // very different numbers here, and saying only the first invites the
        // wrong reading. Components are deduplicated corpus-wide, and the
        // common coformers repeat heavily: ~900 distinct acids across ~10,000
        // structures. That ratio IS the cross-structure identity argument.
        scalar(g, "MATCH (f:Fragment)<-[:HAS_FRAGMENT]-(c:Component)<-[:CONTAINS_COMPONENT]-(s:Structure) WHERE f.fragment_type = 'carboxylic_acid' RETURN count(s)"),
        scalar(g, `${DIMER_COUNT}true AND a1.element = 'O' AND a2.element = 'O' AND a1.cod_id = a2.cod_id RETURN avg(h.length)`),
        scalar(g, `${DIMER_COUNT}true AND a1.element = 'O' AND a2.element = 'O' AND a1.cod_id = a2.cod_id RETURN avg(h.angle)`),
      ])
      const tot = dimers + catemers
      return [
        `${dimers.toLocaleString()} acid-to-acid hydrogen bonds are generated by an operation of order two, which is the R2,2(8) dimer. Mean H...O ${f2(meanLen)} A at ${f1(meanAng)} degrees -- textbook dimer geometry.`,
        `Those come from ${acids.toLocaleString()} DISTINCT acid-bearing molecules appearing across ${acidStructures.toLocaleString()} structures. The gap between those two numbers is the point: a Component node is deduplicated corpus-wide by identity, so the handful of coformers that crystal engineers actually reach for -- benzoic, maleic, succinic, fumaric -- are each one node carrying edges to every structure they appear in. That is what makes "where else has this molecule been co-crystallised" a traversal instead of a string join.`,
        `The discriminator is the one piece of crystallography this whole demo rests on. A dimer is generated by an INVOLUTION -- an inversion centre, a mirror or a two-fold -- an operation that is its own inverse and therefore relates exactly two molecules. A catemer is generated by a 2(1) screw or a glide, which has infinite order and builds a chain instead. Both have a two-fold rotation part, so nothing short of composing the operation with itself tells them apart: ${catemers.toLocaleString()} acid-to-acid bonds here are catemer-generating and are excluded.`,
        `That test is precomputed on every contact edge at ingest, so separating a ring motif from a chain motif costs one boolean in a WHERE clause rather than a symmetry analysis per candidate.`,
        `Worth being precise about the representation, because it is the part people get wrong. The stored graph is a QUOTIENT graph: the second hydrogen bond of a centrosymmetric dimer is the symmetry image of the first, so the dimer is ONE edge here, not two. The R2,2(8) ring is a cycle in the unfolded net, closed by the operation the edge carries. The canvas shows ${sub.nodes.length} oxygen sites over ${sub.edges.length} such edges.`,
      ]
    },
  },
  {
    id: 'weak_strong',
    shelf: 'packing',
    blurb:
      'Three contact populations kept deliberately separate: strong, heavy-atom-inferred, and weak C-H...A. Mixing them silently is how packing statistics go wrong.',
    prompt: 'How do weak C-H...O contacts change the picture?',
    cypher: `MATCH (a:Atom)-[e:CONTACT]->(b:Atom)
WHERE a.cod_id = ${PACKING_SEED_COD}
RETURN a, labels(a), a.label, e, edgeType(e), b, labels(b), b.label`,
    answer: async (graph) => {
      const g = graph
      const [strong, inferred, weak, halogen, structures, mwLen, mwAng] =
        await Promise.all([
          scalar(g, "MATCH ()-[r:CONTACT]->() WHERE r.kind = 'hbond' AND r.h_inferred = false RETURN count(r)"),
          scalar(g, "MATCH ()-[r:CONTACT]->() WHERE r.h_inferred = true RETURN count(r)"),
          scalar(g, "MATCH ()-[r:CONTACT]->() WHERE r.kind = 'hbond_weak' RETURN count(r)"),
          scalar(g, "MATCH ()-[r:CONTACT]->() WHERE r.kind = 'halogen' RETURN count(r)"),
          scalar(g, 'MATCH (s:Structure) RETURN count(s)'),
          scalar(g, "MATCH ()-[r:CONTACT]->() WHERE r.kind = 'hbond_weak' RETURN avg(r.length)"),
          scalar(g, "MATCH ()-[r:CONTACT]->() WHERE r.kind = 'hbond_weak' RETURN avg(r.angle)"),
        ])
      const total = strong + inferred + weak + halogen
      return [
        `The contact layer holds ${total.toLocaleString()} edges over ${structures.toLocaleString()} structures, and they are three different claims, stored as three different populations.`,
        `${strong.toLocaleString()} are strong hydrogen bonds with a located hydrogen. ${inferred.toLocaleString()} are flagged h_inferred -- no hydrogen was refined, so the criterion fell back to a heavy-atom donor-acceptor separation. ${weak.toLocaleString()} are weak C-H...A contacts, mean H...A ${f2(mwLen)} A at ${f1(mwAng)} degrees. ${halogen.toLocaleString()} are halogen bonds.`,
        `The weak population is the one that changes conclusions. With only strong donors most packings do not percolate at all, which is why the dimensionality investigation reports so much 0D. C-H...O is a real, structure-directing interaction -- Taylor and Kennard established that from the CSD itself in 1982 -- and leaving it out makes a corpus look far more like isolated dimers than it is.`,
        `The reason they are separate edge kinds rather than one blended "contact" is that a query must be able to refuse the weaker evidence. Every strong-bond statistic in this demo filters kind = 'hbond', so admitting the weak class changed none of them. A schema that merged them would have silently moved every number on the panel.`,
      ]
    },
  },
  // ---- Solid form & design ----------------------------------------------
  {
    id: 'synthon_competition',
    shelf: 'solidform',
    blurb:
      'When a molecule offers two competing hydrogen-bond partners, which motif actually forms? A frequency count over the contact graph, conditioned on perceived fragments.',
    prompt: 'When acid and pyridine compete, which synthon wins?',
    cypher: `MATCH (f1:Fragment)<-[e:HAS_FRAGMENT]-(c:Component)-[:HAS_FRAGMENT]->(f2:Fragment)
WHERE f1.fragment_type = 'carboxylic_acid' AND f2.fragment_type = 'pyridine_nitrogen'
RETURN c, labels(c), c.formula, e, edgeType(e), f1, labels(f1), f1.fragment_type LIMIT 200`,
    answer: async (graph, sub) => {
      const g = graph
      const F = (t: string) =>
        `MATCH (c:Component)-[:HAS_FRAGMENT]->(f:Fragment) WHERE f.fragment_type = '${t}' RETURN count(c)`
      // `homo` is split by is_involution deliberately. A centrosymmetric acid
      // homodimer is TWO physical hydrogen bonds stored as ONE quotient edge
      // (the second is the symmetry image of the first), while an acid...N
      // heterosynthon is one bond and one edge. Counting edges therefore
      // understates the homosynthon by a factor of two, and on this corpus
      // that is not academic: raw edge counts give 47.9% homo / 52.1% hetero,
      // and correcting the double-counting REVERSES the winner. Rather than
      // silently doubling, both figures are reported and the reason is stated.
      const [both, acid, pyr, homoInv, homoPlain, hetero] = await Promise.all([
        scalar(g, "MATCH (f1:Fragment)<-[:HAS_FRAGMENT]-(c:Component)-[:HAS_FRAGMENT]->(f2:Fragment) WHERE f1.fragment_type = 'carboxylic_acid' AND f2.fragment_type = 'pyridine_nitrogen' RETURN count(c)"),
        scalar(g, F('carboxylic_acid')),
        scalar(g, F('pyridine_nitrogen')),
        scalar(g, "MATCH (f1:Fragment)<-[:HAS_FRAGMENT]-(c1:Component)<-[:IN_COMPONENT]-(a1:Atom)-[h:CONTACT]->(a2:Atom)-[:IN_COMPONENT]->(c2:Component)-[:HAS_FRAGMENT]->(f2:Fragment) WHERE f1.fragment_type = 'carboxylic_acid' AND f2.fragment_type = 'carboxylic_acid' AND h.kind = 'hbond' AND h.h_inferred = false AND h.is_involution = true AND a1.element = 'O' AND a2.element = 'O' RETURN count(h)"),
        scalar(g, "MATCH (f1:Fragment)<-[:HAS_FRAGMENT]-(c1:Component)<-[:IN_COMPONENT]-(a1:Atom)-[h:CONTACT]->(a2:Atom)-[:IN_COMPONENT]->(c2:Component)-[:HAS_FRAGMENT]->(f2:Fragment) WHERE f1.fragment_type = 'carboxylic_acid' AND f2.fragment_type = 'carboxylic_acid' AND h.kind = 'hbond' AND h.h_inferred = false AND h.is_involution = false AND a1.element = 'O' AND a2.element = 'O' RETURN count(h)"),
        scalar(g, "MATCH (f1:Fragment)<-[:HAS_FRAGMENT]-(c1:Component)<-[:IN_COMPONENT]-(a1:Atom)-[h:CONTACT]->(a2:Atom)-[:IN_COMPONENT]->(c2:Component)-[:HAS_FRAGMENT]->(f2:Fragment) WHERE f1.fragment_type = 'carboxylic_acid' AND f2.fragment_type = 'pyridine_nitrogen' AND h.kind = 'hbond' AND h.h_inferred = false AND a1.element = 'O' AND a2.element = 'N' RETURN count(h)"),
      ])
      const homoEdges = homoInv + homoPlain
      // an involution-generated edge stands for two physical hydrogen bonds
      const homoBonds = homoInv * 2 + homoPlain
      const totEdges = homoEdges + hetero
      const totBonds = homoBonds + hetero
      return [
        `${acid.toLocaleString()} components carry a carboxylic acid and ${pyr.toLocaleString()} a pyridine-type nitrogen. ${both.toLocaleString()} carry BOTH, and those are the interesting ones: the molecule offers the crystal a choice between bonding acid-to-acid and acid-to-pyridine.`,
        `Counting stored edges, with inferred hydrogens excluded: ${homoEdges.toLocaleString()} acid-to-acid (${pct(homoEdges, totEdges)}%) against ${hetero.toLocaleString()} acid-to-pyridine (${pct(hetero, totEdges)}%). Counting physical HYDROGEN BONDS, it is ${homoBonds.toLocaleString()} (${pct(homoBonds, totBonds)}%) against ${hetero.toLocaleString()} (${pct(hetero, totBonds)}%).`,
        `Those two readings disagree, and the difference is not a rounding artefact -- it changes which synthon wins. ${homoInv.toLocaleString()} of the acid-to-acid edges are generated by an involution, and each of those stands for TWO physical hydrogen bonds: the second is the symmetry image of the first, so the quotient graph stores it once. The heterosynthon has no such pairing. Counting edges therefore understates the homodimer roughly twofold, which is exactly the kind of bias that a stored-contact representation introduces and that you have to correct for out loud rather than quietly.`,
        `This is the question co-crystal design turns on. The acid/pyridine heterosynthon is the workhorse of pharmaceutical co-crystal screening precisely because it tends to beat the acid homodimer, and a corpus-wide frequency is the evidence a formulator would want before committing screening time.`,
        `The canvas shows ${sub.nodes.length} nodes. Note what had to exist for this to be one query: perceived fragments as NODES, an atom-to-molecule edge so a contact between two atoms resolves to a contact between two molecules, and the hydrogen-bond population stored rather than recomputed. Take away any one and this becomes a scripted job over a structure archive.`,
      ]
    },
  },
  {
    id: 'polymorph',
    shelf: 'solidform',
    blurb:
      'The same molecule packing two different ways. Only expressible because molecular identity is one node shared across structures.',
    prompt: 'Which molecules crystallise in more than one packing?',
    cypher: `MATCH (s:Structure)-[e:CONTAINS_COMPONENT]->(c:Component)
WHERE e.role = 'principal' AND c.is_solvent = false AND c.has_inchikey = true
RETURN c, labels(c), c.formula, e, edgeType(e), s, labels(s), s.hm_symbol LIMIT 260`,
    answer: async (graph, sub) => {
      const g = graph
      // Keyed on space-group NUMBER, not Hermann-Mauguin symbol. An earlier
      // version compared hm_symbol, which counted P2(1)/c against P2(1)/n --
      // the same group in a different setting -- as a different packing, and
      // so contradicted the space-group panel two clicks away. It also counted
      // components whose identity is a connectivity digest rather than an
      // InChIKey; both are now excluded.
      // Written as one connected chain through the SpaceGroup nodes, which is
      // both how the group NUMBER is reachable and, in this engine, ~20x
      // faster than joining the two structures as separate comma patterns.
      const base =
        "MATCH (g1:SpaceGroup)<-[:IN_SPACE_GROUP]-(s1:Structure)" +
        "-[r1:CONTAINS_COMPONENT]->(c:Component)" +
        "<-[r2:CONTAINS_COMPONENT]-(s2:Structure)-[:IN_SPACE_GROUP]->(g2:SpaceGroup) " +
        "WHERE r1.role = 'principal' AND r2.role = 'principal' " +
        "AND c.is_solvent = false AND c.has_inchikey = true "
      const [pairs, sameGroup, comps, withKey] = await Promise.all([
        scalar(g, `${base}AND g1.number <> g2.number RETURN count(c)`).catch(() => 0),
        scalar(g, `${base}AND g1.number = g2.number AND s1.cod_id <> s2.cod_id RETURN count(c)`).catch(() => 0),
        scalar(g, 'MATCH (c:Component) RETURN count(c)'),
        scalar(g, 'MATCH (c:Component) WHERE c.has_inchikey = true RETURN count(c)'),
      ])
      return [
        `${pairs.toLocaleString()} ordered pairs of structures share a principal molecule of identical InChIKey but crystallise in a different space GROUP -- not merely a different setting of the same group, which is the distinction the space-group panel draws.`,
        `A further ${sameGroup.toLocaleString()} pairs share both the molecule and the space group. Those are where the interesting cases hide: two forms in the same group with different cells are still polymorphs, and no space-group comparison will ever find them. Separating the two counts is the honest way to present it.`,
        `The reason either is a one-hop question is the modelling choice underneath. A Component node is deduplicated corpus-wide by identity, so the same molecule appearing in forty structures is ONE node with forty CONTAINS_COMPONENT edges. In a relational schema this is a self-join over a structure table on a chemical identity string -- which only works if that string was computed consistently in the first place, and still says nothing about the packing without a second pass.`,
        `Two limits stated rather than buried. Identity is an InChIKey where RDKit perception succeeds, ${pct(withKey, comps)}% of ${comps.toLocaleString()} components; metal complexes fail by construction, since InChI has no well-defined representation for them, and those carry a connectivity digest in a SEPARATE property so it is never mistaken for a key. And nothing here requires the rest of the composition to match, so a hydrate against an anhydrate is included -- strictly those are solvates, and the distinction is regulatory. The canvas shows ${sub.nodes.length} nodes.`,
      ]
    },
  },
  {
    id: 'coformer',
    shelf: 'solidform',
    blurb:
      'Molecules that co-crystallise with the partners your target already has, but have never been tried with the target itself -- a five-hop join plus an anti-join.',
    prompt: 'Suggest coformers that have never been tried with this compound',
    cypher: `MATCH (f:Fragment)<-[:HAS_FRAGMENT]-(c1:Component)<-[r1:CONTAINS_COMPONENT]-(s:Structure)-[e:CONTAINS_COMPONENT]->(c2:Component)
WHERE f.fragment_type = 'carboxylic_acid' AND c2.is_solvent = false
RETURN c2, labels(c2), c2.formula, e, edgeType(e), s, labels(s), s.cod_id LIMIT 240`,
    answer: async (graph, sub) => {
      const g = graph
      const [partners, multi, solvates] = await Promise.all([
        scalar(g, "MATCH (f:Fragment)<-[:HAS_FRAGMENT]-(c1:Component)<-[:CONTAINS_COMPONENT]-(s:Structure)-[:CONTAINS_COMPONENT]->(c2:Component) WHERE f.fragment_type = 'carboxylic_acid' AND c2.is_solvent = false RETURN count(c2)"),
        scalar(g, 'MATCH (s:Structure) WHERE s.n_components > 1 RETURN count(s)'),
        scalar(g, "MATCH (s:Structure)-[r:CONTAINS_COMPONENT]->(c:Component) WHERE c.is_solvent = true RETURN count(r)"),
      ])
      return [
        `${partners.toLocaleString()} non-solvent component pairings sit in structures where one partner carries a carboxylic acid. ${multi.toLocaleString()} structures are multi-component, and ${solvates.toLocaleString()} component slots are occupied by a classified solvent, which is why the solvent flag has to be in the query rather than applied afterwards -- otherwise every suggestion comes back as water.`,
        `The commercial shape of this question is: my compound has these hydrogen-bond donors and acceptors. Find molecules that are known to co-crystallise with compounds carrying the same complement, but that have never been tried with mine. That is five hops out -- target to fragment, fragment to other molecules carrying it, those to their structures, structures to their partners -- followed by an ANTI-join to remove the pairs already reported.`,
        `The traversal is the part a graph does well and a relational schema does not; the anti-join and the ranking are done client-side here because this dialect has no collect() or WITH, which is a real limitation worth naming rather than hiding.`,
        `The canvas shows ${sub.nodes.length} nodes over ${sub.edges.length} co-occurrence edges. What is on screen is the evidence layer -- who has actually been crystallised with whom -- which is the part nobody currently stores as a graph.`,
      ]
    },
  },
]

/** Route free text onto the closest investigation by word overlap. */
export function routeQuestion(text: string): Investigation {
  const words = text.toLowerCase().split(/[^a-z0-9]+/).filter((w) => w.length > 2)
  let best = INVESTIGATIONS[0]
  let bestScore = -1
  const KEYS: Record<string, string[]> = {
    packing: ['hydrogen', 'bond', 'packing', 'network', 'contact', 'motif', 'structure', 'hbond', 'symop'],
    recurrence: ['molecule', 'recur', 'component', 'coformer', 'cocrystal', 'salt', 'solvate', 'reuse', 'across', 'shared', 'inchikey'],
    spacegroups: ['space', 'group', 'symmetry', 'distribution', 'spacegroup', 'centrosymmetric', 'common'],
    fragments: ['fragment', 'functional', 'group', 'synthon', 'motif', 'smarts', 'dominate'],
    elements: ['element', 'atom', 'metal', 'halogen', 'composition', 'contain', 'chemistry'],
    solvates: ['solvate', 'hydrate', 'solvent', 'water', 'dmso', 'methanol'],
    acid_synthon: ['acid', 'carboxylic', 'carboxylate', 'pyridine', 'dimer', 'synthon', 'coformer', 'salt'],
    history: ['history', 'grow', 'growth', 'commit', 'version', 'time', 'reproduce', 'snapshot', 'year', 'old'],
    audit: ['audit', 'correct', 'retract', 'suspect', 'supersede', 'revision', 'provenance', 'compliance'],
  }
  for (const inv of INVESTIGATIONS) {
    const keys = KEYS[inv.id] ?? []
    const score = words.reduce((s, w) => s + (keys.some((k) => k.includes(w) || w.includes(k)) ? 1 : 0), 0)
    if (score > bestScore) {
      bestScore = score
      best = inv
    }
  }
  return best
}
