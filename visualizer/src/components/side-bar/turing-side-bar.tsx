import logo from '@assets/imgs/logo.svg'
import type { FC } from 'react'
import { Icon } from '@blueprintjs/core'
import type { BlueprintIcons_16Id } from '@blueprintjs/icons/lib/esm/generated/16px/blueprint-icons-16'
import { TuringSideBarItem } from './turing-side-bar-item'
import { TuringTooltip } from '@/components/base/turing-tooltip'
import { useAppStore } from '@/stores/app.store'
import { useVisStore, useCanvasStore } from '@/stores'
import { useSelectedChips } from '@/components/turing-bar/use-selected-chips'

// A sidebar entry that routes into the viewer on a specific graph and opens the
// crystallography studio panel.
const StudioNavItem: FC<{
  icon: BlueprintIcons_16Id
  label: string
  graph: string
  active: boolean
  onOpen: () => void
}> = ({ icon, label, active, onOpen }) => {
  const className = `${
    active
      ? 'sidebar-item-bg-gradient bg-grey-600 !text-content-primary '
      : 'bg-grey-800 !text-content-fourth hover:bg-grey-700 '
  }flex h-10 w-10 items-center justify-center rounded-[4px] transition-colors cursor-pointer outline-none`
  return (
    <TuringTooltip content={label} interactionKind="hover-target" placement="right">
      <div className={className} onClick={onOpen} onKeyDown={onOpen}>
        <Icon icon={icon} />
      </div>
    </TuringTooltip>
  )
}

export const TuringSideBar = () => {
  const isCrystal = useVisStore((s) => s.isCrystalOpen)
  const setCrystal = useVisStore((s) => s.setCrystalOpen)
  const setGraphName = useAppStore((s) => s.setGraphName)
  const graphName = useAppStore((s) => s.graphName)
  const setPage = useAppStore((s) => s.setPage)
  const setSidebarHidden = useAppStore((s) => s.setSidebarHidden)
  const entityCache = useVisStore((s) => s.entityCache)
  const neighbourhood = useVisStore((s) => s.neighbourhood)
  const hiddenNodes = useVisStore((s) => s.hiddenNodes)
  const setRenderedGraph = useVisStore((s) => s.setRenderedGraph)
  const turingActions = useCanvasStore((s) => s.actions)
  const unselectAllChips = useSelectedChips((s) => s.unselectAllChips)

  // Route into the viewer on a graph and open the crystallography studio.
  const open = (graph: string) => {
    setPage('viewer')
    // Switching graphs: wipe the canvas + per-id caches so the new graph can't
    // inherit stale nodes from the previous one (internal ids collide across
    // graphs and entityCache is keyed by bare id). Mirrors the graph dropdown.
    if (graph !== graphName) {
      neighbourhood.reset(graph)
      hiddenNodes.clear()
      entityCache.edges.clear()
      entityCache.nodes.clear()
      unselectAllChips()
      turingActions.reset()
      setRenderedGraph('')
    }
    setCrystal(true)
    setGraphName(graph)
  }

  return (
    <div className="border-grey-900 bg-grey-800 flex h-full w-[64px] flex-col items-center space-y-1 border">
      <img aria-label="turing-logo" src={logo} className="m-3 h-[36px] w-[36px]" />
      <TuringSideBarItem iconName="graph" page="viewer" name="Viewer" />
      <StudioNavItem icon="lab-test" label="Crystallography" graph="cod_slice_v2" active={isCrystal} onOpen={() => open('cod_slice_v2')} />
      <div className="flex-grow" />
      <TuringTooltip content="Hide sidebar" interactionKind="hover-target" placement="right">
        <div
          onClick={() => setSidebarHidden(true)}
          onKeyDown={() => setSidebarHidden(true)}
          className="bg-grey-800 text-content-fourth hover:bg-grey-700 mb-2 flex h-10 w-10 cursor-pointer items-center justify-center rounded-[4px] outline-none transition-colors"
        >
          <Icon icon="double-chevron-left" />
        </div>
      </TuringTooltip>
    </div>
  )
}
