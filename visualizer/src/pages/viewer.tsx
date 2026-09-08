import { TuringBottomToolbar } from '@/components/viewer/menus/bottom-toolbar'
import { TuringTopToolBar } from '@/components/viewer/menus/top-toolbar'
import { HierarchyBrowser } from '@/components/viewer/hierarchy-browser'
import { TuringNodeInspector } from '@/components/viewer/node-inspector'
import { useAppStore, useCanvasStore, useVisStore } from '@/stores'
import { type FC, useCallback, useEffect, useRef, useState } from 'react'
import { type NodeData, TuringCanvas, type TuringUserEvents, useTuringContext } from '@turingcanvas'

import { TuringContextMenuType } from '@/components/viewer/menus/turing-context-menu-type'

import {
  TuringContextMenu,
  type TuringContextMenuInfo,
} from '@/components/viewer/menus/turing-context-menu'

import useGraphEntities from '@/hooks/use-graph-entities'
import { CrystalPanel } from '@/components/viewer/crystal'
import { CrystalChat } from '@/components/viewer/crystal/chat'
import { CrystalClusterLabels } from '@/components/viewer/crystal/labels'
import { CRYSTAL_GRAPHS } from '@/components/viewer/crystal/model'

// Size of each batch submitted to the canvas per animation frame. Tuned so a
// single batch fits comfortably in one frame on mid-range hardware.
const BATCH_SIZE = 500

const yieldToBrowser = () =>
  new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))

const GraphCanvasData: FC = () => {
  const turing = useTuringContext()

  const { data } = useGraphEntities()

  const neighbourhood = useVisStore((state) => state.neighbourhood)
  const setGraphLoading = useVisStore((state) => state.setGraphLoading)
  const turingResetStates = useCanvasStore((state) => state.resetStates)

  useEffect(() => {
    if (!data) return

    const newCanvasNodes = [...data.graphNodes.values()]
      .filter((n) => !turing.instance.nodeMap.has(n.id))
      .map((n) => ({
        id: n.id,
        primary: neighbourhood.has(n.id),
        data: n as NodeData,
      }))

    const newCanvasEdges = [...data.graphEdges.values()]
      .filter((e) => !turing.instance.edgeMap.has(e[0]))
      .map((e) => ({
        id: e[0],
        src: e[1],
        tgt: e[2],
        data: e as NodeData,
      }))

    const deletedNodes = turing.instance.nodes.filter((n) => !data.graphNodes.has(n.id))
    const deletedEdges = turing.instance.edges.filter((e) => !data.graphEdges.has(e.id))

    const didChange =
      newCanvasNodes.length !== 0 ||
      newCanvasEdges.length !== 0 ||
      deletedNodes.length !== 0 ||
      deletedEdges.length !== 0

    // No-op effect runs fire during mid-pipeline store updates (e.g. after
    // neighbourhood.reset when both canvas and data are empty). They must
    // not clear graphLoading — that stays owned by useCypherQuery.onMutate
    // until the final render batch completes here.
    if (!didChange) return

    let cancelled = false

    const run = async () => {
      setGraphLoading(true)
      try {
        for (let i = 0; i < newCanvasNodes.length; i += BATCH_SIZE) {
          if (cancelled) return
          turing.instance.addNodes(newCanvasNodes.slice(i, i + BATCH_SIZE))
          if (newCanvasNodes.length > BATCH_SIZE) await yieldToBrowser()
        }

        for (let i = 0; i < newCanvasEdges.length; i += BATCH_SIZE) {
          if (cancelled) return
          turing.instance.addEdges(newCanvasEdges.slice(i, i + BATCH_SIZE))
          if (newCanvasEdges.length > BATCH_SIZE) await yieldToBrowser()
        }

        for (const node of deletedNodes) {
          if (cancelled) return
          turing.instance.delNode(node.id)
        }
        for (const edge of deletedEdges) {
          if (cancelled) return
          turing.instance.delEdge(edge.id)
        }

        for (const node of turing.instance.nodes) {
          if (neighbourhood.has(node.id)) turing.instance.makePrimary(node)
          else turing.instance.makeSecondary(node)
        }

        turingResetStates('nodeMap', 'nodes', 'selectedNodes', 'edges', 'edgeMap')
      } finally {
        if (!cancelled) setGraphLoading(false)
      }
    }

    run()

    return () => {
      cancelled = true
    }
  }, [turingResetStates, turing, data, neighbourhood, setGraphLoading])

  return <></>
}

interface GraphCanvasProps {
  setContextMenuInfo: (info: TuringContextMenuInfo | undefined) => void
}

const GraphCanvas: FC<GraphCanvasProps> = (props) => {
  const { setContextMenuInfo } = props
  const inspectNode = useVisStore((state) => state.inspectNode)
  const graphNameForCanvas = useAppStore((s) => s.graphName)
  // The crystallography studio drives the canvas itself. The stock data
  // pipeline fetches EVERY node and edge in the graph, which is hopeless at
  // 547k atoms / 1.3M edges, and it would also wipe the manually painted
  // subgraph on its next sync.
  const crystalView = !!graphNameForCanvas && CRYSTAL_GRAPHS.has(graphNameForCanvas)

  const closeInspectNodePanel = useVisStore((state) => state.closeInspectNodePanel)
  const { newNeighbours, add: addNeighbour } = useVisStore((state) => state.neighbourhood)

  useEffect(() => {
    closeInspectNodePanel()
  }, [closeInspectNodePanel])

  const crystalViewRef = useRef(crystalView)
  crystalViewRef.current = crystalView

  const events = useRef<Partial<TuringUserEvents>>({
    canvassingleclick: (e) => {
      // The crystallography studio shows its own detail panel: these nodes are
      // painted manually so they never enter entityCache, and the stock
      // inspector would come up empty.
      if (crystalViewRef.current) {
        window.dispatchEvent(
          new CustomEvent('crystal-node-click', { detail: { id: e.detail.n?.id ?? null } })
        )
        return
      }

      if (!e.detail.n) {
        closeInspectNodePanel()
        return
      }

      inspectNode(e.detail.n.id)
    },

    canvasdoubleclick: (e) => {
      const n = e.detail.n
      if (!n) return

      if (n.isPrimary()) {
        newNeighbours([n.id])
        return
      }

      addNeighbour([n.id])
    },

    canvascontextmenu: (e) => {
      const node = e.detail.n

      if (node) {
        setContextMenuInfo({
          type: TuringContextMenuType.NODE,
          offset: {
            left: e.detail.event.clientX,
            top: e.detail.event.clientY,
          },
          node,
        })
        return
      }

      setContextMenuInfo({
        type: TuringContextMenuType.CANVAS,
        offset: {
          left: e.detail.event.clientX,
          top: e.detail.event.clientY,
        },
      })
    },
  })

  return (
    <>
      {!crystalView && <GraphCanvasData />}
      <TuringCanvas
        id="turing-canvas-1"
        className="bg-visualizer-pattern relative"
        events={events.current}
      />
    </>
  )
}

export const TViewerPage = () => {
  const [contextMenuInfo, setContextMenuInfo] = useState<TuringContextMenuInfo | undefined>()
  const crystalOpen = useVisStore((s) => s.isCrystalOpen)
  const setCrystalOpen = useVisStore((s) => s.setCrystalOpen)
  const setHierarchyOpen = useVisStore((s) => s.setHierarchyBrowserOpen)
  const closeContextMenu = useCallback(() => setContextMenuInfo(undefined), [])
  const graphName = useAppStore((s) => s.graphName)
  // the curated defense consoles (supply-chain catalog/chat + the operational
  // defense/dependency-exposure page) drive the graph themselves, so hide the
  // graph toolbar (query bar, gravity, add node, etc.) for a clean presentation.
  // Landing on a crystallography graph (including via ?graph=) should show the
  // studio without having to click the sidebar first.
  useEffect(() => {
    if (!!graphName && CRYSTAL_GRAPHS.has(graphName)) {
      if (!crystalOpen) setCrystalOpen(true)
      // The hierarchy browser is a flex sibling, so when it opens it shrinks the
      // canvas and shoves this studio's absolutely-positioned panels around. It
      // is redundant here anyway -- the legend and the detail panel cover it.
      setHierarchyOpen(false)
    }
  }, [graphName, crystalOpen, setCrystalOpen, setHierarchyOpen])

  // The crystallography studio drives the canvas itself, so the generic graph
  // toolbar (query bar, gravity, add node) is hidden for its graphs.
  const hideToolbar = !!graphName && CRYSTAL_GRAPHS.has(graphName)

  return (
    <div className="relative flex flex-1 flex-row overflow-hidden">
      <TuringContextMenu close={closeContextMenu} info={contextMenuInfo} />
      <GraphCanvas setContextMenuInfo={setContextMenuInfo} />
      <div id="cm" />
      {!hideToolbar && <TuringTopToolBar />}
      <TuringBottomToolbar />
      <TuringNodeInspector />
      {/* The hierarchy browser re-opens itself whenever a graph loads, and as a
          flex sibling it shrinks the canvas and displaces this studio's panels.
          The legend and detail panel replace it here, so it is simply not
          mounted on the crystallography graphs. */}
      {!(!!graphName && CRYSTAL_GRAPHS.has(graphName)) && <HierarchyBrowser />}
      <CrystalClusterLabels />
      <CrystalPanel />
      <CrystalChat />
    </div>
  )
}
