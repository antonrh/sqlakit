import { beforeEach, expect, test } from "bun:test"

import { stream } from "@/lib/source"
import { useStore } from "@/lib/store"

/** The browser's stream, with the parts this page uses and nothing else. */
class Stream {
  static opened: Stream[] = []
  static readonly CLOSED = 2

  readyState = 0
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onerror: (() => void) | null = null

  constructor(readonly url: string) {
    Stream.opened.push(this)
  }
}

Object.defineProperty(globalThis, "EventSource", { value: Stream, configurable: true })

const last = () => Stream.opened[Stream.opened.length - 1]!

beforeEach(() => {
  Stream.opened = []
  useStore.setState({ runs: [], held: [], paused: false, live: "yes" })
})

test("a stream that stops leaves the page saying so", () => {
  stream()
  last().readyState = 0 // the browser will try again itself
  last().onerror?.()

  expect(useStore.getState().live).toBe("no")
  expect(Stream.opened).toHaveLength(1)
})

test("a server that comes back is live again", () => {
  stream()
  last().onerror?.()
  last().onopen?.()

  expect(useStore.getState().live).toBe("yes")
})

test("a stream the browser gave up on is opened again", async () => {
  stream(1)
  last().readyState = Stream.CLOSED
  last().onerror?.()

  await Bun.sleep(20)

  expect(Stream.opened.length).toBeGreaterThan(1)

  last().onopen?.()

  expect(useStore.getState().live).toBe("yes")
})

test("what arrives on the stream is kept", () => {
  stream()
  last().onmessage?.({
    data: JSON.stringify({
      app: "web",
      tags: [],
      label: "GET /users",
      count: 0,
      at: 0,
      milliseconds: 0,
      duplicates: 0,
      statements: [],
    }),
  })

  expect(useStore.getState().runs.map((run) => run.label)).toEqual(["GET /users"])
})
