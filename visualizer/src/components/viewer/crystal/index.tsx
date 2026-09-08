// Crystallography studio -- left panel.
//
// Dark grey column with three sections: a corpus overview, a colour legend for
// the node types, and the details of whatever node is selected on the canvas.
// All text is DM Sans (loaded in index.html).
import { type FC, useCallback, useEffect, useRef, useState } from 'react'
import { Icon } from '@blueprintjs/core'
import { useAppStore, useVisStore } from '@/stores'
import { useTuringContext } from '@turingcanvas'
import {
  CRYSTAL_GRAPHS,
  INVESTIGATIONS,
  LABEL_STYLES,
  type NodeDetail,
  type Overview,
  loadNodeDetail,
  loadOverview,
  loadSubgraph,
} from './model'
import { paintSubgraph } from './canvas'

const DM = "'DM Sans', system-ui, sans-serif"

const PANEL_W = 372

const Section: FC<{ title: string; children: React.ReactNode; right?: React.ReactNode }> = ({
  title,
  children,
  right,
}) => (
  <div style={{ borderTop: '1px solid #24272e', padding: '14px 18px 16px' }}>
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        marginBottom: 10,
      }}
    >
      <div
        style={{
          fontFamily: DM,
          fontSize: 11,
          letterSpacing: '0.09em',
          textTransform: 'uppercase',
          color: '#6f7787',
          fontWeight: 500,
        }}
      >
        {title}
      </div>
      {right}
    </div>
    {children}
  </div>
)

const Stat: FC<{ label: string; value: string; hint?: string }> = ({ label, value, hint }) => (
  <div style={{ background: '#1b1e24', borderRadius: 6, padding: '9px 11px' }}>
    <div
      style={{
        fontFamily: DM,
        fontSize: 17,
        fontWeight: 500,
        color: '#e4e8f0',
        fontVariantNumeric: 'tabular-nums',
        lineHeight: 1.15,
      }}
    >
      {value}
    </div>
    <div style={{ fontFamily: DM, fontSize: 11, color: '#7d8494', marginTop: 2 }}>{label}</div>
    {hint && (
      <div style={{ fontFamily: DM, fontSize: 10, color: '#5c636f', marginTop: 2 }}>{hint}</div>
    )}
  </div>
)

export const CrystalPanel: FC = () => {
  const graphName = useAppStore((s) => s.graphName)
  const isOpen = useVisStore((s) => s.isCrystalOpen)
  const turing = useTuringContext()

  const [overview, setOverview] = useState<Overview | null>(null)
  const [detail, setDetail] = useState<NodeDetail | null>(null)
  const [loadingDetail, setLoadingDetail] = useState(false)
  const [err, setErr] = useState('')
  const bootRef = useRef('')

  const active = isOpen && !!graphName && CRYSTAL_GRAPHS.has(graphName)

  // ---- overview + an initial subgraph so the canvas is never blank --------
  useEffect(() => {
    if (!active || !graphName) return
    if (bootRef.current === graphName) return
    bootRef.current = graphName
    let cancelled = false
    ;(async () => {
      try {
        const ov = await loadOverview(graphName)
        if (!cancelled) setOverview(ov)
        const sub = await loadSubgraph(graphName, INVESTIGATIONS[0].cypher)
        if (!cancelled) paintSubgraph(turing, sub)
      } catch (e) {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [active, graphName, turing])

  // ---- canvas selection --------------------------------------------------
  const onNodeClick = useCallback(
    async (ev: Event) => {
      const id = (ev as CustomEvent<{ id: number | null }>).detail?.id
      if (!graphName) return
      if (id == null) {
        setDetail(null)
        return
      }
      setLoadingDetail(true)
      try {
        const d = await loadNodeDetail(graphName, id)
        setDetail(d)
      } catch {
        setDetail(null)
      } finally {
        setLoadingDetail(false)
      }
    },
    [graphName]
  )

  useEffect(() => {
    window.addEventListener('crystal-node-click', onNodeClick)
    return () => window.removeEventListener('crystal-node-click', onNodeClick)
  }, [onNodeClick])

  if (!active) return null

  const n = (v: number | undefined) => (v ?? 0).toLocaleString()

  return (
    <div
      style={{
        position: 'absolute',
        left: 0,
        top: 0,
        bottom: 0,
        // Pin the width three ways and hide horizontal overflow. Without this
        // the panel visibly jumped when a node was selected: long unbreakable
        // values (an InChIKey, a DOI) widened the flex rows, and the vertical
        // scrollbar appearing as the detail section grew shifted the content
        // again. scrollbarGutter reserves the gutter permanently so the second
        // effect cannot happen either.
        width: PANEL_W,
        minWidth: PANEL_W,
        maxWidth: PANEL_W,
        boxSizing: 'border-box',
        background: '#15181d',
        borderRight: '1px solid #24272e',
        overflowY: 'auto',
        overflowX: 'hidden',
        scrollbarGutter: 'stable',
        zIndex: 20,
        fontFamily: DM,
      }}
    >
      {/* header */}
      <div style={{ padding: '18px 18px 14px' }}>
        <div style={{ fontFamily: DM, fontSize: 15, fontWeight: 600, color: '#eef1f6' }}>
          Crystallography
        </div>
        <div style={{ fontFamily: DM, fontSize: 12, color: '#7d8494', marginTop: 3, lineHeight: 1.45 }}>
          Crystallography Open Database — CrystEngComm, Crystal Growth &amp; Design and
          Acta Cryst. E, modelled as a graph.
        </div>
      </div>

      {err && (
        <div
          style={{
            margin: '0 18px 12px',
            padding: '9px 11px',
            borderRadius: 6,
            background: '#2a1a1c',
            border: '1px solid #4a2528',
            color: '#f6a6a0',
            fontFamily: DM,
            fontSize: 11.5,
          }}
        >
          {err}
        </div>
      )}

      {/* ---------------- overview ---------------- */}
      <Section title="Corpus overview">
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
          <Stat label="Structures" value={n(overview?.structures)} />
          <Stat label="Atomic sites" value={n(overview?.atoms)} />
          <Stat label="Components" value={n(overview?.components)} hint="molecules & ions" />
          <Stat label="Covalent bonds" value={n(overview?.bonds)} />
          <Stat
            label="Intermolecular contacts"
            value={n(overview?.contacts)}
            hint="not stored by COD"
          />
          <Stat label="Space groups" value={n(overview?.spaceGroups)} />
        </div>

        {overview && (
          <div
            style={{
              marginTop: 10,
              padding: '10px 11px',
              background: '#1b1e24',
              borderRadius: 6,
              fontFamily: DM,
              fontSize: 11.5,
              color: '#8b93a3',
              lineHeight: 1.6,
            }}
          >
            <div>
              <span style={{ color: '#cdd4e0' }}>{n(overview.hbonds)}</span> hydrogen bonds,{' '}
              <span style={{ color: '#cdd4e0' }}>{n(overview.halogen)}</span> halogen bonds
            </div>
            <div>
              mean H···A{' '}
              <span style={{ color: '#cdd4e0', fontVariantNumeric: 'tabular-nums' }}>
                {overview.meanHA.toFixed(2)} Å
              </span>{' '}
              at{' '}
              <span style={{ color: '#cdd4e0', fontVariantNumeric: 'tabular-nums' }}>
                {overview.meanAngle.toFixed(1)}°
              </span>
            </div>
            <div style={{ marginTop: 5, color: '#6f7787', fontSize: 11 }}>
              {n(overview.hbondsInferred)} hydrogen bonds are h_inferred — no refined H
              position, so a heavy-atom cutoff was used. Kept flagged, never mixed in
              silently.
            </div>
          </div>
        )}
      </Section>

      {/* ---------------- legend ---------------- */}
      <Section title="Node types">
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {LABEL_STYLES.map((s) => (
            <div key={s.label} style={{ display: 'flex', alignItems: 'flex-start', gap: 9 }}>
              <span
                style={{
                  width: 10,
                  height: 10,
                  borderRadius: '50%',
                  background: s.css,
                  marginTop: 4,
                  flex: '0 0 auto',
                }}
              />
              <div style={{ minWidth: 0 }}>
                <div style={{ fontFamily: DM, fontSize: 12.5, color: '#dfe4ee', fontWeight: 500 }}>
                  {s.label}
                </div>
                <div style={{ fontFamily: DM, fontSize: 11, color: '#6f7787', lineHeight: 1.4 }}>
                  {s.blurb}
                </div>
              </div>
            </div>
          ))}
        </div>
      </Section>

      {/* ---------------- selected node ---------------- */}
      <Section
        title="Selected node"
        right={
          detail && (
            <span
              style={{
                fontFamily: DM,
                fontSize: 10.5,
                color: '#0d1013',
                background: LABEL_STYLES.find((s) => s.label === detail.label)?.css ?? '#8b93a3',
                padding: '2px 7px',
                borderRadius: 3,
                fontWeight: 600,
              }}
            >
              {detail.label}
            </span>
          )
        }
      >
        {loadingDetail && (
          <div style={{ fontFamily: DM, fontSize: 12, color: '#6f7787' }}>Reading node…</div>
        )}

        {!loadingDetail && !detail && (
          <div
            style={{
              fontFamily: DM,
              fontSize: 12,
              color: '#6f7787',
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              lineHeight: 1.5,
            }}
          >
            <Icon icon="select" size={13} />
            Click a node on the canvas to inspect it.
          </div>
        )}

        {!loadingDetail && detail && (
          <>
            <div
              style={{
                fontFamily: DM,
                fontSize: 14.5,
                fontWeight: 600,
                color: '#eef1f6',
                marginBottom: 9,
                overflowWrap: 'anywhere',
                minWidth: 0,
              }}
            >
              {detail.title}
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 0 }}>
              {detail.rows.map((r) => (
                <div
                  key={r.k}
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    gap: 12,
                    padding: '5px 0',
                    borderBottom: '1px solid #1e2127',
                    minWidth: 0,
                  }}
                >
                  <span
                    style={{
                      fontFamily: DM,
                      fontSize: 11.5,
                      color: '#7d8494',
                      flex: '0 0 auto',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {r.k}
                  </span>
                  <span
                    style={{
                      fontFamily: DM,
                      fontSize: 11.5,
                      color: '#dfe4ee',
                      textAlign: 'right',
                      fontVariantNumeric: 'tabular-nums',
                      // `anywhere` (not break-word) is what stops a long
                      // InChIKey from establishing a wide min-content size
                      minWidth: 0,
                      overflowWrap: 'anywhere',
                    }}
                  >
                    {r.v}
                  </span>
                </div>
              ))}
            </div>
            {detail.note && (
              <div
                style={{
                  marginTop: 10,
                  fontFamily: DM,
                  fontSize: 11,
                  color: '#6f7787',
                  lineHeight: 1.55,
                }}
              >
                {detail.note}
              </div>
            )}
          </>
        )}
      </Section>
    </div>
  )
}

export default CrystalPanel
