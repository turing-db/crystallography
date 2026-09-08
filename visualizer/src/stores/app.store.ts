import { create } from 'zustand'

export type PageType = 'viewer' | 'help'
export type ThemeType = 'dark' | 'light'

export type AppStore = {
  theme: ThemeType
  setTheme: (v: ThemeType) => void

  page: PageType
  setPage: (v: PageType) => void

  graphName: string | undefined
  setGraphName: (v: string | undefined) => void

  sidebarHidden: boolean
  setSidebarHidden: (v: boolean) => void
}

export const useAppStore = create<AppStore>((set) => ({
  theme: 'dark' as ThemeType,
  setTheme: (v: ThemeType) => set({ theme: v }),

  page: 'viewer' as PageType,
  setPage: (v: PageType) => set({ page: v }),

  // Boot straight into the crystallography graph. The picker still works, but
  // the schema fetch (labels / edge types) happens on mount, and with no graph
  // selected it returns nothing and the canvas has no labels to colour or
  // render -- the UI reports "No labels found." and stays blank even after a
  // graph is picked. Overridable with ?graph=<name> for switching datasets.
  graphName:
    new URLSearchParams(window.location.search).get("graph") || "cod_slice_v2",
  setGraphName: (v: string | undefined) => set({ graphName: v }),

  sidebarHidden: false,
  setSidebarHidden: (v: boolean) => set({ sidebarHidden: v }),
}))

export default useAppStore
