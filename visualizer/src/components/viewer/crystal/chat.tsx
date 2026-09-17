// Crystallography studio -- investigation chat, top right.
//
// Rounded panel in IBM Plex Sans. Offers three suggested investigations; each
// runs real Cypher against the graph, paints the result on the canvas, and then
// streams back a narrative answer.
//
// The answer text is assembled from live follow-up queries (counts and
// averages over the actual graph) and streamed word by word so it reads like a
// generated response. It is templated prose over real numbers -- there is no
// language model behind it, and nothing in it is invented.
import { type FC, useCallback, useEffect, useRef, useState } from 'react'
import { Icon } from '@blueprintjs/core'
import { useAppStore, useVisStore } from '@/stores'
import { isCypherQuery } from '@/utils/is-cypher'
import { useTuringContext } from '@turingcanvas'
import {
  CRYSTAL_GRAPHS,
  INVESTIGATIONS,
  SHELVES,
  type Investigation,
  type Snapshot,
  VERSIONED_GRAPH,
  loadLedger,
  loadSubgraph,
  routeQuestion,
  runRawCypher,
  type RawResult,
} from './model'
import { paintSubgraph } from './canvas'

const PLEX = "'IBM Plex Sans', system-ui, sans-serif"

type Phase = 'idle' | 'querying' | 'painting' | 'answering' | 'done'

interface Turn {
  question: string
  cypher: string
  nodes: number
  edges: number
  ms: number
  paragraphs: string[]
  shown: number // words revealed so far, across the whole answer
  /** set when the investigation can be replayed across commits */
  inv?: Investigation
  /** set when the user typed Cypher instead of picking an investigation */
  raw?: RawResult
}

const GREETING = 'Good afternoon, what would you like to investigate'

export const CrystalChat: FC = () => {
  const graphName = useAppStore((s) => s.graphName)
  const isOpen = useVisStore((s) => s.isCrystalOpen)
  const turing = useTuringContext()

  const [collapsed, setCollapsed] = useState(false)
  const [input, setInput] = useState('')
  const [phase, setPhase] = useState<Phase>('idle')
  const [turn, setTurn] = useState<Turn | null>(null)
  const [showCypher, setShowCypher] = useState(false)
  const [err, setErr] = useState('')
  const timerRef = useRef<number | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)

  // commit toggle
  const [ledger, setLedger] = useState<Snapshot[]>([])
  const [activeCommit, setActiveCommit] = useState<string | null>(null)
  const [metrics, setMetrics] = useState<{ label: string; value: string }[]>([])
  const [tlBusy, setTlBusy] = useState(false)

  const active = isOpen && !!graphName && CRYSTAL_GRAPHS.has(graphName)

  useEffect(() => () => {
    if (timerRef.current) window.clearInterval(timerRef.current)
  }, [])

  const run = useCallback(
    async (inv: Investigation, question: string) => {
      if (!graphName) return
      if (timerRef.current) window.clearInterval(timerRef.current)
      setErr('')
      setShowCypher(false)
      setPhase('querying')
      setMetrics([])
      setActiveCommit(null)
      setTurn({
        question,
        cypher: inv.cypher,
        nodes: 0,
        edges: 0,
        ms: 0,
        paragraphs: [],
        shown: 0,
        inv,
      })
      if (inv.timeline) {
        loadLedger().then(setLedger).catch(() => setLedger([]))
      }

      // An investigation may target another graph -- the versioning ones read
      // the ledger and the chronologically-committed corpus, not the graph the
      // studio is otherwise viewing.
      const target = inv.graph ?? graphName
      const t0 = performance.now()
      try {
        const sub = await loadSubgraph(target, inv.cypher)
        const ms = performance.now() - t0

        setPhase('painting')
        paintSubgraph(turing, sub)

        setPhase('answering')
        const paragraphs = await inv.answer(target, sub)
        const totalWords = paragraphs.reduce((s, p) => s + p.split(' ').length, 0)

        setTurn({
          question,
          cypher: inv.cypher,
          nodes: sub.nodes.length,
          edges: sub.edges.length,
          ms,
          paragraphs,
          shown: 0,
          inv,
        })

        // stream the answer
        let shown = 0
        timerRef.current = window.setInterval(() => {
          shown += 3
          setTurn((t) => (t ? { ...t, shown } : t))
          if (shown >= totalWords) {
            if (timerRef.current) window.clearInterval(timerRef.current)
            setPhase('done')
          }
        }, 45)
      } catch (e) {
        setErr(e instanceof Error ? e.message : String(e))
        setPhase('idle')
      }
    },
    [graphName, turing]
  )

  /**
   * Execute Cypher the user typed. Goes through this studio's own painter
   * rather than the stock toolbar's pipeline, which is disabled on these
   * graphs because it would fetch all 4.7M atoms and overwrite the canvas.
   */
  const runRaw = useCallback(
    async (query: string) => {
      if (!graphName) return
      if (timerRef.current) window.clearInterval(timerRef.current)
      setErr('')
      setShowCypher(true)
      setPhase('querying')
      setMetrics([])
      setActiveCommit(null)
      setTurn({
        question: 'Your query',
        cypher: query,
        nodes: 0,
        edges: 0,
        ms: 0,
        paragraphs: [],
        shown: 0,
      })
      try {
        const res = await runRawCypher(graphName, query)
        if (res.kind === 'graph' && res.sub) {
          setPhase('painting')
          paintSubgraph(turing, res.sub)
        }
        setTurn((t) =>
          t
            ? {
                ...t,
                nodes: res.sub?.nodes.length ?? 0,
                edges: res.sub?.edges.length ?? 0,
                ms: res.ms,
                raw: res,
              }
            : t
        )
        setPhase('done')
      } catch (e) {
        setErr(e instanceof Error ? e.message : String(e))
        setPhase('idle')
      }
    },
    [graphName, turing]
  )

  /** Re-run the current investigation against one commit and repaint. */
  const replayAt = useCallback(
    async (snap: Snapshot | null) => {
      const inv = turn?.inv
      if (!inv?.timeline) return
      setTlBusy(true)
      setErr('')
      setActiveCommit(snap ? snap.commit : null)
      try {
        const cypher = inv.timeline.cypher ?? inv.cypher
        const sub = await loadSubgraph(
          VERSIONED_GRAPH,
          cypher,
          snap ? snap.commit : undefined
        )
        paintSubgraph(turing, sub)
        setTurn((t) =>
          t ? { ...t, nodes: sub.nodes.length, edges: sub.edges.length } : t
        )
        setMetrics(await inv.timeline.metrics(snap ? snap.commit : ''))

        // The prose has to move with the chips, or the paragraph describes
        // HEAD while the chips describe the selected commit.
        if (snap && inv.timeline.answerAt) {
          const paras = await inv.timeline.answerAt(snap.commit, snap.tag)
          const words = paras.reduce((n, x) => n + x.split(' ').length, 0)
          setTurn((t) => (t ? { ...t, paragraphs: paras, shown: words } : t))
        } else if (!snap) {
          // back to HEAD: restore the investigation's own narrative
          const target = inv.graph ?? graphName ?? ''
          const paras = await inv.answer(target, sub)
          const words = paras.reduce((n, x) => n + x.split(' ').length, 0)
          setTurn((t) => (t ? { ...t, paragraphs: paras, shown: words } : t))
        }
      } catch (e) {
        setErr(e instanceof Error ? e.message : String(e))
      } finally {
        setTlBusy(false)
      }
    },
    [turn?.inv, turn?.cypher, turing, graphName]
  )

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight
  }, [turn?.shown, turn?.question])

  if (!active) return null

  const submit = () => {
    const q = input.trim()
    if (!q) return
    setInput('')
    // Anything starting MATCH / CALL / RETURN / ... is meant as Cypher; the
    // rest is routed to the nearest investigation by word overlap. Same
    // detection the stock toolbar uses, so the two behave consistently.
    if (isCypherQuery(q)) runRaw(q)
    else run(routeQuestion(q), q)
  }

  // reveal the answer progressively across paragraphs
  const rendered: string[] = []
  if (turn) {
    let budget = turn.shown
    for (const p of turn.paragraphs) {
      const w = p.split(' ')
      if (budget <= 0) break
      rendered.push(w.slice(0, budget).join(' '))
      budget -= w.length
    }
  }

  const activeSnap = ledger.find((x) => x.commit === activeCommit) ?? null
  const busy = phase === 'querying' || phase === 'painting' || phase === 'answering'
  const statusText =
    phase === 'querying'
      ? 'Querying the graph'
      : phase === 'painting'
        ? 'Rendering the subgraph'
        : phase === 'answering'
          ? 'Composing'
          : ''

  return (
    <div
      style={{
        position: 'absolute',
        top: 16,
        right: 16,
        width: 428,
        maxHeight: 'calc(100% - 32px)',
        display: 'flex',
        flexDirection: 'column',
        background: '#181b21',
        border: '1px solid #2a2e37',
        borderRadius: 14,
        boxShadow: '0 18px 46px rgba(0,0,0,.5)',
        zIndex: 30,
        overflow: 'hidden',
        fontFamily: PLEX,
      }}
    >
      {/* header */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 9,
          padding: '12px 14px',
          borderBottom: collapsed ? 'none' : '1px solid #23272f',
        }}
      >
        <span
          style={{
            width: 7,
            height: 7,
            borderRadius: '50%',
            background: busy ? '#f2c14e' : '#7fe0b0',
            flex: '0 0 auto',
          }}
        />
        <div style={{ fontFamily: PLEX, fontSize: 13, fontWeight: 500, color: '#e6eaf1', flex: 1 }}>
          Investigate
        </div>
        <button
          type="button"
          onClick={() => setCollapsed((c) => !c)}
          style={{
            background: 'transparent',
            border: 'none',
            color: '#7d8494',
            cursor: 'pointer',
            padding: 2,
            display: 'flex',
          }}
        >
          <Icon icon={collapsed ? 'chevron-down' : 'chevron-up'} size={14} />
        </button>
      </div>

      {!collapsed && (
        <>
          <div ref={scrollRef} style={{ overflowY: 'auto', padding: '14px', flex: 1 }}>
            {!turn && (
              <>
                <div
                  style={{
                    fontFamily: PLEX,
                    fontSize: 19,
                    fontWeight: 400,
                    color: '#eef1f6',
                    lineHeight: 1.35,
                    marginBottom: 16,
                  }}
                >
                  {GREETING}
                </div>
                <div
                  style={{
                    fontFamily: PLEX,
                    fontSize: 11,
                    letterSpacing: '0.08em',
                    textTransform: 'uppercase',
                    color: '#6f7787',
                    marginBottom: 9,
                  }}
                >
                  Query library
                </div>
                {/* Grouped rather than one flat list: a CCDC room splits into
                    crystallographers, solid-form/design people and the database
                    team, and each shelf is aimed at one of them. Every entry
                    carries a one-line statement of what it DEMONSTRATES, since
                    the prompt alone does not say why the question is hard. */}
                {SHELVES.map((shelf) => {
                  const items = INVESTIGATIONS.filter((i) => i.shelf === shelf.id)
                  if (!items.length) return null
                  return (
                    <div key={shelf.id} style={{ marginBottom: 16 }}>
                      <div
                        style={{
                          fontFamily: PLEX,
                          fontSize: 12,
                          fontWeight: 600,
                          color: '#aab3c4',
                          marginBottom: 2,
                        }}
                      >
                        {shelf.title}
                      </div>
                      <div
                        style={{
                          fontFamily: PLEX,
                          fontSize: 11,
                          color: '#6f7787',
                          lineHeight: 1.4,
                          marginBottom: 8,
                        }}
                      >
                        {shelf.note}
                      </div>
                      <div
                        style={{ display: 'flex', flexDirection: 'column', gap: 7 }}
                      >
                        {items.map((inv) => (
                          <button
                            key={inv.id}
                            type="button"
                            onClick={() => run(inv, inv.prompt)}
                            style={{
                              textAlign: 'left',
                              background: '#1f232b',
                              border: '1px solid #2b303a',
                              borderRadius: 10,
                              padding: '10px 12px',
                              color: '#d6dce8',
                              fontFamily: PLEX,
                              fontSize: 13,
                              lineHeight: 1.45,
                              cursor: 'pointer',
                            }}
                          >
                            <div>{inv.prompt}</div>
                            <div
                              style={{
                                fontSize: 11,
                                color: '#7f8799',
                                lineHeight: 1.4,
                                marginTop: 4,
                              }}
                            >
                              {inv.blurb}
                            </div>
                          </button>
                        ))}
                      </div>
                    </div>
                  )
                })}
              </>
            )}

            {turn && (
              <>
                {/* the question */}
                <div
                  style={{
                    background: '#232833',
                    borderRadius: 10,
                    padding: '9px 12px',
                    marginBottom: 12,
                    fontFamily: PLEX,
                    fontSize: 13,
                    color: '#e6eaf1',
                    lineHeight: 1.45,
                  }}
                >
                  {turn.question}
                </div>

                {/* what the graph returned */}
                {turn.nodes > 0 && (
                  <div
                    style={{
                      display: 'flex',
                      gap: 14,
                      marginBottom: 10,
                      fontFamily: PLEX,
                      fontSize: 11.5,
                      color: '#7d8494',
                      fontVariantNumeric: 'tabular-nums',
                    }}
                  >
                    <span>{turn.nodes.toLocaleString()} nodes</span>
                    <span>{turn.edges.toLocaleString()} edges</span>
                    <span>{turn.ms.toFixed(0)} ms</span>
                    <button
                      type="button"
                      onClick={() => setShowCypher((v) => !v)}
                      style={{
                        marginLeft: 'auto',
                        background: 'transparent',
                        border: 'none',
                        color: '#6ea8fe',
                        cursor: 'pointer',
                        fontFamily: PLEX,
                        fontSize: 11.5,
                        padding: 0,
                      }}
                    >
                      {showCypher ? 'hide Cypher' : 'show Cypher'}
                    </button>
                  </div>
                )}

                {showCypher && (
                  <pre
                    style={{
                      background: '#12151a',
                      border: '1px solid #23272f',
                      borderRadius: 8,
                      padding: '9px 11px',
                      marginBottom: 12,
                      fontSize: 10.5,
                      lineHeight: 1.55,
                      color: '#9fb2cc',
                      overflowX: 'auto',
                      fontFamily: "'IBM Plex Mono', ui-monospace, monospace",
                      whiteSpace: 'pre-wrap',
                    }}
                  >
                    {turn.cypher}
                  </pre>
                )}

                {/* Put the query in the box so it can be edited and re-run.
                    Reading the Cypher is worth something; changing one
                    predicate and watching the canvas move is worth much more
                    to an audience that writes queries for a living. */}
                {showCypher && (
                  <button
                    type="button"
                    onClick={() => setInput(turn.cypher)}
                    style={{
                      background: 'transparent',
                      border: '1px solid #2b303a',
                      borderRadius: 7,
                      color: '#8fa5c4',
                      fontFamily: PLEX,
                      fontSize: 11.5,
                      padding: '5px 10px',
                      marginBottom: 12,
                      cursor: 'pointer',
                    }}
                  >
                    edit &amp; run this query
                  </button>
                )}

                {turn.raw?.kind === 'table' && (
                  <div
                    style={{
                      marginBottom: 12,
                      overflowX: 'auto',
                      border: '1px solid #23272f',
                      borderRadius: 8,
                    }}
                  >
                    <table
                      style={{
                        borderCollapse: 'collapse',
                        width: '100%',
                        fontFamily: "'IBM Plex Mono', ui-monospace, monospace",
                        fontSize: 11,
                      }}
                    >
                      <thead>
                        <tr>
                          {turn.raw.columns?.map((c) => (
                            <th
                              key={c}
                              style={{
                                textAlign: 'left',
                                padding: '7px 10px',
                                borderBottom: '1px solid #2b303a',
                                color: '#7d8494',
                                fontWeight: 400,
                                whiteSpace: 'nowrap',
                              }}
                            >
                              {c}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {turn.raw.rows?.map((r, i) => (
                          <tr key={i}>
                            {r.map((v, j) => (
                              <td
                                key={j}
                                style={{
                                  padding: '6px 10px',
                                  borderBottom: '1px solid #1d2128',
                                  color: '#cdd4e0',
                                  whiteSpace: 'nowrap',
                                  fontVariantNumeric: 'tabular-nums',
                                }}
                              >
                                {v}
                              </td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    {turn.raw.truncated && (
                      <div
                        style={{
                          padding: '7px 10px',
                          fontFamily: PLEX,
                          fontSize: 11,
                          color: '#6f7787',
                        }}
                      >
                        first 40 rows shown
                      </div>
                    )}
                  </div>
                )}

                {turn.raw?.kind === 'graph' && turn.nodes === 0 && (
                  <div
                    style={{
                      marginBottom: 12,
                      fontFamily: PLEX,
                      fontSize: 12.5,
                      color: '#7d8494',
                    }}
                  >
                    The query ran and returned no rows.
                  </div>
                )}

                {busy && (
                  <div
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 7,
                      fontFamily: PLEX,
                      fontSize: 12,
                      color: '#7d8494',
                      marginBottom: 8,
                    }}
                  >
                    <span className="crystal-blink">▮</span>
                    {statusText}
                  </div>
                )}

                {/* ---- commit toggle: replay this query across the corpus history ---- */}
                {turn.inv?.timeline && ledger.length > 0 && (
                  <div
                    style={{
                      background: '#12151a',
                      border: '1px solid #23272f',
                      borderRadius: 10,
                      padding: '10px 11px',
                      marginBottom: 12,
                    }}
                  >
                    <div
                      style={{
                        display: 'flex',
                        alignItems: 'baseline',
                        gap: 8,
                        marginBottom: 9,
                      }}
                    >
                      <span
                        style={{
                          fontFamily: PLEX,
                          fontSize: 10.5,
                          letterSpacing: '0.08em',
                          textTransform: 'uppercase',
                          color: '#6f7787',
                        }}
                      >
                        Corpus as of
                      </span>
                      <span
                        style={{
                          fontFamily: PLEX,
                          fontSize: 13,
                          fontWeight: 500,
                          color: activeSnap ? '#ffab3d' : '#7fe0b0',
                        }}
                      >
                        {activeSnap ? activeSnap.tag : 'HEAD (all years)'}
                      </span>
                      {tlBusy && (
                        <span
                          className="crystal-blink"
                          style={{ color: '#f2c14e', fontSize: 11 }}
                        >
                          replaying…
                        </span>
                      )}
                    </div>

                    {/* one tick per commit, oldest to newest, plus HEAD */}
                    <div style={{ display: 'flex', alignItems: 'flex-end', gap: 2 }}>
                      {ledger.map((sn) => {
                        const on = activeCommit === sn.commit
                        // bar height encodes how much the corpus had grown
                        const frac = sn.total / (ledger[ledger.length - 1].total || 1)
                        return (
                          <button
                            key={sn.commit}
                            type="button"
                            title={`${sn.tag} — ${sn.total.toLocaleString()} structures (+${sn.added.toLocaleString()})`}
                            onClick={() => replayAt(sn)}
                            style={{
                              flex: 1,
                              minWidth: 0,
                              height: 8 + Math.round(frac * 22),
                              background: on ? '#ffab3d' : '#2c323e',
                              border: 'none',
                              borderRadius: 2,
                              cursor: 'pointer',
                              padding: 0,
                            }}
                          />
                        )
                      })}
                      <button
                        type="button"
                        title="HEAD — the whole corpus"
                        onClick={() => replayAt(null)}
                        style={{
                          marginLeft: 4,
                          height: 30,
                          padding: '0 7px',
                          background: activeCommit === null ? '#2b4a3c' : '#2c323e',
                          border: `1px solid ${activeCommit === null ? '#7fe0b0' : '#343b47'}`,
                          borderRadius: 3,
                          color: activeCommit === null ? '#7fe0b0' : '#8b93a3',
                          fontFamily: PLEX,
                          fontSize: 10.5,
                          cursor: 'pointer',
                        }}
                      >
                        HEAD
                      </button>
                    </div>
                    <div
                      style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        marginTop: 4,
                        fontFamily: PLEX,
                        fontSize: 9.5,
                        color: '#5c636f',
                        fontVariantNumeric: 'tabular-nums',
                      }}
                    >
                      <span>{ledger[0]?.year}</span>
                      <span>{ledger[Math.floor(ledger.length / 2)]?.year}</span>
                      <span>{ledger[ledger.length - 1]?.year}</span>
                    </div>

                    {metrics.length > 0 && (
                      <div
                        style={{
                          display: 'grid',
                          gridTemplateColumns: '1fr 1fr',
                          gap: 6,
                          marginTop: 10,
                        }}
                      >
                        {metrics.map((m) => (
                          <div
                            key={m.label}
                            style={{
                              background: '#181b21',
                              borderRadius: 5,
                              padding: '6px 8px',
                            }}
                          >
                            <div
                              style={{
                                fontFamily: PLEX,
                                fontSize: 13,
                                fontWeight: 500,
                                color: '#e6eaf1',
                                fontVariantNumeric: 'tabular-nums',
                              }}
                            >
                              {m.value}
                            </div>
                            <div style={{ fontFamily: PLEX, fontSize: 10, color: '#7d8494' }}>
                              {m.label}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}

                    <div
                      style={{
                        fontFamily: PLEX,
                        fontSize: 10.5,
                        color: '#5c636f',
                        marginTop: 8,
                        lineHeight: 1.5,
                      }}
                    >
                      Each tick is a real commit, one per publication year. Selecting one
                      re-runs this same query against the graph as it stood then.
                    </div>
                  </div>
                )}

                {rendered.map((p, i) => (
                  <p
                    key={i}
                    style={{
                      fontFamily: PLEX,
                      fontSize: 13,
                      lineHeight: 1.62,
                      color: '#c8cfdc',
                      margin: '0 0 10px',
                    }}
                  >
                    {p}
                  </p>
                ))}

                {phase === 'done' && (
                  <button
                    type="button"
                    onClick={() => {
                      setTurn(null)
                      setPhase('idle')
                    }}
                    style={{
                      background: 'transparent',
                      border: '1px solid #2b303a',
                      borderRadius: 8,
                      padding: '6px 11px',
                      color: '#8b93a3',
                      fontFamily: PLEX,
                      fontSize: 12,
                      cursor: 'pointer',
                    }}
                  >
                    Ask something else
                  </button>
                )}
              </>
            )}

            {err && (
              <div
                style={{
                  marginTop: 10,
                  padding: '9px 11px',
                  borderRadius: 8,
                  background: '#2a1a1c',
                  border: '1px solid #4a2528',
                  color: '#f6a6a0',
                  fontFamily: PLEX,
                  fontSize: 11.5,
                  whiteSpace: 'pre-wrap',
                }}
              >
                {err}
              </div>
            )}
          </div>

          {/* input */}
          <div style={{ padding: 12, borderTop: '1px solid #23272f', display: 'flex', gap: 8 }}>
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') submit()
              }}
              placeholder="Ask a question, or type Cypher…"
              style={{
                flex: 1,
                background: '#12151a',
                border: '1px solid #2a2e37',
                borderRadius: 9,
                padding: '8px 11px',
                color: '#e6eaf1',
                fontFamily: PLEX,
                fontSize: 13,
                outline: 'none',
              }}
            />
            <button
              type="button"
              onClick={submit}
              disabled={busy}
              style={{
                background: busy ? '#232833' : '#2b3a55',
                border: '1px solid #34405a',
                borderRadius: 9,
                width: 38,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: '#cfe0ff',
                cursor: busy ? 'default' : 'pointer',
              }}
            >
              <Icon icon="arrow-right" size={14} />
            </button>
          </div>
        </>
      )}
    </div>
  )
}

export default CrystalChat
