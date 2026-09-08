// Painting a crystallography subgraph onto the WebGL canvas.
//
// This studio drives the canvas MANUALLY rather than through the stock data
// pipeline. The stock pipeline fetches every node and edge in the graph, which
// is hopeless here: cod_slice_v2 holds 547k atoms and 1.3M edges. Instead each
// investigation runs one explicit query and we render only what it returned.
//
// Two traps worth knowing about, both learned the hard way on the other studio
// pages:
//   * Every TuringNode is constructed at (0,0), so d3's force layout starts
//     from N coincident points, repulsion is degenerate and the layout crawls.
//     We seed a phyllotaxis spiral before letting it relax.
//   * Synthetic node data must carry a `properties` object -- the canvas label
//     hooks call Object.keys(data.properties) across all nodes and throw on
//     undefined.
import type { TuringContext } from '@turingcanvas'
import { EDGE_COLOR, STYLE_BY_LABEL, type Subgraph } from './model'
import { computeHubs, useCrystalStore } from './store'

const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5))

/** Spread nodes on a phyllotaxis spiral so the force layout has somewhere to start. */
function seedPositions(turing: TuringContext, count: number) {
  const sim = turing.instance.simulation
  const simNodes = sim.getNodes?.() ?? []
  const radius = 12 + Math.sqrt(Math.max(count, 1)) * 3.2
  simNodes.forEach((n: { x?: number; y?: number; index?: number }, i: number) => {
    const t = i / Math.max(simNodes.length, 1)
    const r = radius * Math.sqrt(t)
    const a = i * GOLDEN_ANGLE
    n.x = r * Math.cos(a)
    n.y = r * Math.sin(a)
  })
}

export function paintSubgraph(turing: TuringContext, sub: Subgraph) {
  const inst = turing.instance
  inst.reset()

  // tell the cluster-label overlay which nodes are the hubs of this view
  useCrystalStore.getState().setHubs(computeHubs(sub))

  if (!sub.nodes.length) return

  inst.addNodes(
    sub.nodes.map((n) => ({
      id: n.id,
      primary: true,
      // `properties` must exist or the label hooks crash
      data: { properties: { name: n.name }, __label: n.label, __name: n.name } as never,
    }))
  )

  const present = new Set(sub.nodes.map((n) => n.id))
  inst.addEdges(
    sub.edges
      .filter((e) => present.has(e.src) && present.has(e.tgt))
      .map((e) => ({
        id: e.id,
        src: e.src,
        tgt: e.tgt,
        data: { properties: {}, __edgeType: e.type } as never,
      }))
  )

  // colour + label every node by its crystallographic type
  for (const n of sub.nodes) {
    const node = inst.nodeMap.get(n.id)
    if (!node) continue
    const style = STYLE_BY_LABEL[n.label]
    if (style) inst.setNodeColor(node, style.color)
    if (n.name) inst.setNodeLabel(node, n.name)
  }

  for (const e of sub.edges) {
    const edge = inst.edgeMap?.get(e.id)
    if (!edge) continue
    const c = EDGE_COLOR[e.type]
    if (c !== undefined) inst.setEdgeColor(edge, c)
  }

  seedPositions(turing, sub.nodes.length)

  // Copy the seeds straight onto the rendered nodes so the first frame already
  // shows a spread graph rather than one dot at the origin.
  const sim = inst.simulation
  const simNodes = sim.getNodes?.() ?? []
  for (const sn of simNodes as { x?: number; y?: number; index?: number }[]) {
    if (sn.index === undefined) continue
    const rn = inst.nodes[sn.index]
    if (!rn || sn.x === undefined || sn.y === undefined) continue
    rn.x = sn.x
    rn.y = sn.y
  }
  inst.updatePositions?.()

  // Let the engine's own render loop relax the layout, then frame it. Running a
  // few hundred ticks by hand here instead pushed the nodes so far apart that
  // fitNodes could not bring them back inside its zoom clamp -- the canvas
  // showed a couple of enormous edges and nothing else.
  const ids = sub.nodes.map((n) => n.id)
  const fit = () => {
    try {
      inst.fitNodes(ids)
    } catch {
      inst.fitView?.()
    }
  }
  // Fit at several checkpoints rather than once at the end. In a real browser
  // these are ~80ms apart so the graph snaps into frame immediately and then
  // refines as the layout relaxes; the early ones also mean the view is usable
  // in a throttled tab, where rAF drops to roughly one frame a second.
  const CHECKPOINTS = [5, 15, 30, 60]
  let frames = 0
  const settle = () => {
    frames++
    if (CHECKPOINTS.includes(frames)) fit()
    if (frames < CHECKPOINTS[CHECKPOINTS.length - 1]) requestAnimationFrame(settle)
  }
  requestAnimationFrame(settle)
  // Belt and braces for a hidden tab, where rAF may not run at all.
  window.setTimeout(fit, 900)
}
