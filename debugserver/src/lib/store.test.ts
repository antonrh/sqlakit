import { beforeEach, expect, test } from "bun:test"

import type { Recording } from "@/lib/records"
import { useStore } from "@/lib/store"

// A browser keeps what the reader chose; the runner has nowhere to keep it.
const kept = new Map<string, string>()
Object.defineProperty(globalThis, "localStorage", {
  value: {
    getItem: (name: string) => kept.get(name) ?? null,
    setItem: (name: string, value: string) => kept.set(name, value),
    removeItem: (name: string) => kept.delete(name),
  },
  configurable: true,
})

const arriving = (label: string): Recording => ({
  app: "web",
  tags: [],
  label,
  count: 0,
  at: 0,
  milliseconds: 0,
  duplicates: 0,
  statements: [],
})

beforeEach(() => {
  useStore.setState({ runs: [], held: [], missed: 0, paused: false })
})

test("a recording joins the list as it arrives", () => {
  useStore.getState().keep(arriving("GET /users"))

  expect(useStore.getState().runs.map((run) => run.label)).toEqual(["GET /users"])
})

test("paused holds what arrives, and says how much is waiting", () => {
  useStore.getState().pause()
  useStore.getState().keep(arriving("GET /users"))
  useStore.getState().keep(arriving("POST /posts"))

  const { runs, missed } = useStore.getState()

  expect(runs).toEqual([])
  expect(missed).toBe(2)
})

test("running again shows what was held, in the order it arrived", () => {
  useStore.getState().pause()
  useStore.getState().keep(arriving("GET /users"))
  useStore.getState().pause()

  const { runs, held, missed, paused } = useStore.getState()

  expect(runs.map((run) => run.label)).toEqual(["GET /users"])
  expect(held).toEqual([])
  expect(missed).toBe(0)
  expect(paused).toBe(false)
})

test("clearing forgets what was held as well", () => {
  useStore.getState().pause()
  useStore.getState().keep(arriving("GET /users"))
  useStore.getState().clear()
  useStore.getState().pause()

  expect(useStore.getState().runs).toEqual([])
})
