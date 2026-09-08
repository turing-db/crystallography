// Floating labels over the cluster centres on the canvas.
//
// These views are hub-and-spoke, so the big visual clumps each have one node at
// their centre -- a space group, a fragment, an element. Without a label you can
// see that there are two large clusters and many small ones, but not what they
// are. This projects each hub's world position to screen space every frame and
// parks a small caption above it.
//
// Positions come from instance.projectNode(), which already inverts the
// raycaster's screen-to-NDC mapping and returns CSS pixels.
import { type FC, useEffect, useRef, useState } from 'react'
import { useAppStore, useVisStore } from '@/stores'
import { useTuringContext } from '@turingcanvas'
import { CRYSTAL_GRAPHS } from './model'
import { useCrystalStore } from './store'

const DM = "'DM Sans', system-ui, sans-serif"

//: Two labels closer than this (in CSS pixels) are treated as overlapping.
const MIN_SEP_X = 150
const MIN_SEP_Y = 54

interface Placed {
  id: number
  x: number
  y: number
  name: string
  kind: string
  degree: number
  css: string
}

export const CrystalClusterLabels: FC = () => {
  const graphName = useAppStore((s) => s.graphName)
  const isOpen = useVisStore((s) => s.isCrystalOpen)
  const hubs = useCrystalStore((s) => s.hubs)
  const turing = useTuringContext()
  const [placed, setPlaced] = useState<Placed[]>([])
  const raf = useRef<number | null>(null)

  const active = isOpen && !!graphName && CRYSTAL_GRAPHS.has(graphName)

  useEffect(() => {
    if (!active || hubs.length === 0) {
      setPlaced([])
      return
    }
    let stop = false
    const loop = () => {
      if (stop) return
      const out: Placed[] = []
      for (const h of hubs) {
        const p = turing.instance.projectNode?.(h.id)
        if (!p) continue
        // drop anything projected off-screen; the panel occupies the left edge
        // and the chat the top right
        if (p.x < 400 || p.y < 40) continue
        // Collision avoidance. hubs arrive sorted by degree, so the biggest
        // cluster always wins a contested spot and smaller ones simply go
        // unlabelled -- much more readable than stacking captions on top of
        // each other, which is what happened when every hub got one.
        let clash = false
        for (const q of out) {
          if (Math.abs(q.x - p.x) < MIN_SEP_X && Math.abs(q.y - p.y) < MIN_SEP_Y) {
            clash = true
            break
          }
        }
        if (clash) continue
        out.push({ ...h, x: p.x, y: p.y })
      }
      setPlaced(out)
      raf.current = requestAnimationFrame(loop)
    }
    raf.current = requestAnimationFrame(loop)
    return () => {
      stop = true
      if (raf.current) cancelAnimationFrame(raf.current)
    }
  }, [active, hubs, turing])

  if (!active || placed.length === 0) return null

  return (
    <div
      style={{
        position: 'absolute',
        inset: 0,
        pointerEvents: 'none',
        zIndex: 15,
        overflow: 'hidden',
      }}
    >
      {placed.map((p) => (
        <div
          key={p.id}
          style={{
            position: 'absolute',
            left: p.x,
            top: p.y,
            transform: 'translate(-50%, calc(-100% - 22px))',
            background: 'rgba(18,21,26,0.9)',
            border: `1px solid ${p.css}55`,
            borderLeft: `2px solid ${p.css}`,
            borderRadius: 5,
            padding: '4px 8px',
            whiteSpace: 'nowrap',
            fontFamily: DM,
            backdropFilter: 'blur(2px)',
          }}
        >
          <div style={{ fontSize: 12, fontWeight: 600, color: '#eef1f6', lineHeight: 1.2 }}>
            {p.name}
          </div>
          <div
            style={{
              fontSize: 10,
              color: p.css,
              marginTop: 1,
              letterSpacing: '0.03em',
              fontVariantNumeric: 'tabular-nums',
            }}
          >
            {p.kind} · {p.degree} in view
          </div>
        </div>
      ))}
    </div>
  )
}

export default CrystalClusterLabels
