// Shared state between the studio's panels and the cluster-label overlay.
//
// The overlay has to know which nodes are the hubs of the currently painted
// subgraph, and it is a sibling of the canvas rather than a child of the panel
// that ran the query, so the hub list travels through a small store.
import { create } from 'zustand'
import type { Subgraph } from './model'
import { STYLE_BY_LABEL } from './model'

export interface Hub {
  id: number
  /** node label, e.g. SpaceGroup */
  kind: string
  /** display name, e.g. "P 1 21/c 1" */
  name: string
  /** how many neighbours it has inside the painted subgraph */
  degree: number
  css: string
}

interface CrystalState {
  hubs: Hub[]
  setHubs: (h: Hub[]) => void
  /** one-line description of what is on the canvas right now */
  viewCaption: string
  setViewCaption: (s: string) => void
}

export const useCrystalStore = create<CrystalState>((set) => ({
  hubs: [],
  setHubs: (hubs) => set({ hubs }),
  viewCaption: '',
  setViewCaption: (viewCaption) => set({ viewCaption }),
}))

/**
 * Pick out the cluster centres of a subgraph.
 *
 * These views are overwhelmingly hub-and-spoke -- many Structures around one
 * SpaceGroup, many Components around one Fragment -- so "cluster" and "high
 * degree node" are the same thing here, and no community detection is needed.
 * Anything with a handful of neighbours gets a label; the rest stay quiet so
 * the canvas does not fill up with text.
 */
export function computeHubs(sub: Subgraph, maxLabels = 7, minDegree = 4): Hub[] {
  const degree = new Map<number, number>()
  for (const e of sub.edges) {
    degree.set(e.src, (degree.get(e.src) ?? 0) + 1)
    degree.set(e.tgt, (degree.get(e.tgt) ?? 0) + 1)
  }
  return sub.nodes
    .map((n) => ({
      id: n.id,
      kind: n.label,
      name: n.name || n.label,
      degree: degree.get(n.id) ?? 0,
      css: STYLE_BY_LABEL[n.label]?.css ?? '#8b93a3',
    }))
    .filter((h) => h.degree >= minDegree)
    .sort((a, b) => b.degree - a.degree)
    .slice(0, maxLabels)
}
