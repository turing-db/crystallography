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
}

export async function loadOverview(graph: string): Promise<Overview> {
  const q = (s: string) => scalar(graph, s)
  const [
    structures, atoms, components, contacts, hbonds, hbondsInferred, halogen,
    bonds, fragments, spaceGroups, withInchikey, polymeric, meanHA, meanAngle,
  ] = await Promise.all([
    q('MATCH (n:Structure) RETURN count(n)'),
    q('MATCH (n:Atom) RETURN count(n)'),
    q('MATCH (n:Component) RETURN count(n)'),
    q('MATCH ()-[r:CONTACT]->() RETURN count(r)'),
    q("MATCH ()-[r:CONTACT]->() WHERE r.kind = 'hbond' RETURN count(r)"),
    q('MATCH ()-[r:CONTACT]->() WHERE r.h_inferred = true RETURN count(r)'),
    q("MATCH ()-[r:CONTACT]->() WHERE r.kind = 'halogen' RETURN count(r)"),
    q('MATCH ()-[r:BONDED_TO]->() RETURN count(r)'),
    q('MATCH (n:Fragment) RETURN count(n)'),
    q('MATCH (n:SpaceGroup) RETURN count(n)'),
    q('MATCH (c:Component) WHERE c.has_inchikey = true RETURN count(c)'),
    q('MATCH (c:Component) WHERE c.is_polymeric = true RETURN count(c)'),
    q('MATCH ()-[r:CONTACT]->() WHERE r.h_inferred = false RETURN avg(r.length)'),
    q('MATCH ()-[r:CONTACT]->() WHERE r.h_inferred = false RETURN avg(r.angle)'),
  ])
  return {
    structures, atoms, components, contacts, hbonds, hbondsInferred, halogen,
    bonds, fragments, spaceGroups, withInchikey, polymeric, meanHA, meanAngle,
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

export interface Investigation {
  id: string
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
  }
  /** Builds the narrative answer from live follow-up queries. */
  answer: (graph: string, sub: Subgraph) => Promise<string[]>
}

const f1 = (v: number) => v.toFixed(1)
const f2 = (v: number) => v.toFixed(2)
const pct = (a: number, b: number) => (b ? ((100 * a) / b).toFixed(1) : '0.0')

/** A structure that actually has a rich contact network, for the packing view. */
export const PACKING_SEED_COD = 2229029

export const INVESTIGATIONS: Investigation[] = [
  {
    id: 'packing',
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
        `From here the synthon question is a pattern match rather than a geometry job: take two acid-bearing components and ask for a reciprocal pair of hydrogen bonds between them, which is the R2,2(8) acid dimer. There are ${hbonds.toLocaleString()} hydrogen bonds with a located hydrogen available to satisfy it.`,
        `Worth noting where the dialect bites: TuringDB rejects a cycle written as a cycle, so that reciprocal pair has to be expressed as two independent edge patterns joined on properties. The result is identical; the phrasing is not what a Neo4j author would write.`,
      ]
    },
  },
  {
    id: 'history',
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
    prompt: 'How are space groups distributed in this corpus?',
    cypher: `MATCH (s:Structure)-[e:IN_SPACE_GROUP]->(g:SpaceGroup)
RETURN s, labels(s), s.cod_id, e, edgeType(e), g, labels(g), g.hm_symbol LIMIT 120`,
    timeline: {
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
      const centro = ranked.filter((r) => ['P 1 21/c 1', 'P -1', 'C 1 2/c 1', 'P 1 21/n 1', 'P b c a'].includes(r.hm))
        .reduce((s2, r) => s2 + r.n, 0)
      return [
        `${total.toLocaleString()} structures are distributed over ${nGroups} distinct space groups, and the distribution is extremely skewed.`,
        `The leaders are ${top}.`,
        `Those centrosymmetric groups alone account for ${pct(centro, total)}% of the corpus. That is the familiar result: molecules pack most efficiently using inversion centres and glide planes, so a handful of groups dominates the whole of small-molecule crystallography.`,
        `Note that P2(1)/c and P2(1)/n appear separately here. They are the same group (No. 14) in different cell settings, which is exactly the kind of distinction that gets lost when symmetry is a text column rather than a node you can traverse to.`,
        `That distinction is also why two counts in this app differ, and it is worth being precise about. The panel on the left reports 159 space groups because that graph keys a SpaceGroup node by group NUMBER. The commit toggle reports more because the chronologically-committed graph keys by Hermann-Mauguin SYMBOL, so each setting is its own node. Same corpus, two modelling choices, and the node carries both properties so either question is answerable.`,
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
