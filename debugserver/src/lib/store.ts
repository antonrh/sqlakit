/** What the page is showing, and what the reader chose. */

import { create } from "zustand"
import { persist, createJSONStorage } from "zustand/middleware"

import { received, type Recording, type Run } from "@/lib/records"
import { LAYOUT, type Layout } from "@/lib/sql"

export type Sort = "recent" | "slow" | "many" | "repeated" | "label"

export const PER_PAGE = 25

type State = {
  runs: Run[]
  /** What arrived while paused, which the list gets when it runs again. */
  held: Run[]
  search: string
  sort: Sort
  app: string
  tags: string[]
  page: number
  values: boolean
  fold: boolean
  layout: Layout
  paused: boolean
  missed: number
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
      app: "",
      tags: [],
      page: 0,
      values: false,
      fold: false,
      layout: LAYOUT,
      paused: false,
      missed: 0,
      live: "yes",
      about: "",

      keep: (recording) => {
        const run = received(recording)
        const { paused, held, runs } = get()
        // Paused holds what arrives, so the list the reader is on stays still.
        if (paused) set({ held: [...held, run], missed: held.length + 1 })
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
      clear: () => set({ runs: [], held: [], missed: 0, page: 0 }),
      pause: () => {
        const { paused, runs, held } = get()
        if (paused) set({ paused: false, runs: [...runs, ...held], held: [], missed: 0 })
        else set({ paused: true, missed: 0 })
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
