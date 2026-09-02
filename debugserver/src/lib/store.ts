/** What the page is showing, and what the reader chose. */

import { create } from "zustand"
import { persist, createJSONStorage } from "zustand/middleware"

import { received, type Recording, type Run } from "@/lib/records"
import { LAYOUT, type Layout } from "@/lib/sql"

export type Sort = "recent" | "slow" | "many" | "repeated" | "label"

export const PER_PAGE = 25

/** How much a pause holds before it forgets the oldest of it. */
export const HELD = 100

type State = {
  runs: Run[]
  /** What arrived while paused, which the list gets when it runs again. */
  held: Run[]
  search: string
  sort: Sort
  tags: string[]
  page: number
  values: boolean
  fold: boolean
  layout: Layout
  paused: boolean
  live: "yes" | "no" | "kept"
  about: string
}

type Actions = {
  keep: (recording: Recording) => void
  set: <K extends keyof State>(what: K, value: State[K]) => void
  search_: (text: string) => void
  toggleTag: (tag: string) => void
  clear: () => void
  pause: () => void
}

export const useStore = create<State & Actions>()(
  persist(
    (set, get) => ({
      runs: [],
      held: [],
      search: "",
      sort: "recent",
      tags: [],
      page: 0,
      values: false,
      fold: false,
      layout: LAYOUT,
      paused: false,
      live: "yes",
      about: "",

      keep: (recording) => {
        const run = received(recording)
        const { paused, held, runs } = get()
        // Paused holds what arrives, so the list the reader is on stays still.
        if (paused) set({ held: [...held, run].slice(-HELD) })
        else set({ runs: [...runs, run] })
      },
      set: (what, value) => set({ [what]: value, page: 0 } as never),
      search_: (text) => set({ search: text, page: 0 }),
      toggleTag: (tag) =>
        set({
          tags: get().tags.includes(tag)
            ? get().tags.filter((one) => one !== tag)
            : [...get().tags, tag],
          page: 0,
        }),
      clear: () => set({ runs: [], held: [], page: 0 }),
      pause: () => {
        const { paused, runs, held } = get()
        if (paused) set({ paused: false, runs: [...runs, ...held], held: [] })
        else set({ paused: true })
      },
    }),
    {
      name: "sqlakit",
      storage: createJSONStorage(() => localStorage),
      // What the reader chose, not what the server sent.
      partialize: ({ sort, values, fold, layout }) => ({ sort, values, fold, layout }),
    },
  ),
)
