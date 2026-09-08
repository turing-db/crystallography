import { useAppStore } from '@/stores'
import type React from 'react'
import { Icon } from '@blueprintjs/core'
import { TuringTopBar } from './top-bar/turing-top-bar'
import { TuringSideBar } from './side-bar/turing-side-bar'

type TuringLayoutProps = {
  children?: React.ReactNode
}

export const TuringLayout = (props: TuringLayoutProps) => {
  const theme = useAppStore((state) => state.theme)
  const page = useAppStore((state) => state.page)
  const sidebarHidden = useAppStore((state) => state.sidebarHidden)
  const setSidebarHidden = useAppStore((state) => state.setSidebarHidden)

  return (
    <div
      className={`bp5-${theme} app-scrollbar box-border grid h-screen w-full grid-cols-2 grid-cols-[max-content_1fr] grid-rows-[max-content_1fr] bg-white`}
    >
      {/* first column: keep the cell so the grid layout is preserved even when
          the rail is hidden (it just collapses to zero width) */}
      <div className="row-span-2">{!sidebarHidden && <TuringSideBar />}</div>
      <div><TuringTopBar /></div>
      <div className="relative h-full w-full overflow-hidden">
        {props.children}
        {sidebarHidden && (
          <button
            type="button"
            onClick={() => setSidebarHidden(false)}
            title="Show sidebar"
            className="bg-grey-800 hover:bg-grey-700 border-grey-600 pointer-events-auto absolute top-3 left-3 z-30 flex h-9 w-9 items-center justify-center rounded-md border"
            style={{ color: '#C9CFDE', boxShadow: '0 2px 10px rgba(0,0,0,0.4)' }}
          >
            <Icon icon="menu" size={16} />
          </button>
        )}
      </div>
    </div>
  )
}
