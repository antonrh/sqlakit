import { describe, expect, test } from "bun:test"

import { folded, HOT, left, received, repeats, SLOW, type Recording } from "@/lib/records"

const recording: Recording = {
  app: "web",
  tags: [],
  label: "GET /users",
  count: 3,
  at: 0,
  milliseconds: 3,
  duplicates: 2,
  statements: [
    {
      sql: "SELECT 1 FROM users",
      parameters: null,
      milliseconds: 1,
      database: "default",
      stack: [],
    },
    {
      sql: "SELECT 2 FROM posts",
      parameters: null,
      milliseconds: 1,
      database: "default",
      stack: [],
    },
    {
      sql: "SELECT 2 FROM posts",
      parameters: null,
      milliseconds: 2,
      database: "default",
      stack: [],
    },
  ],
}

describe("what the page adds to a recording", () => {
  const run = received(recording)

  test("an id, and a time when the sender gave none", () => {
    expect(run.id).toBeGreaterThan(0)
    expect(run.at).toBeGreaterThan(0)
  })

  test("what ran, and where", () => {
    expect(run.kinds).toEqual({ select: 3 })
    expect(run.tables).toEqual(["users", "posts"])
    expect(run.databases).toEqual(["default"])
  })

  test("how much of it was slow", () => {
    const quick = received(recording)
    const heavy = received({
      ...recording,
      statements: [
        { ...recording.statements[0]!, milliseconds: SLOW },
        { ...recording.statements[1]!, milliseconds: HOT },
      ],
    })

    expect([quick.slow, quick.hot]).toEqual([0, 0])
    expect([heavy.slow, heavy.hot]).toEqual([2, 1])
  })

  test("the databases that ran it, named, and where each table was touched", () => {
    const across = received({
      ...recording,
      statements: [
        recording.statements[0]!,
        { ...recording.statements[1]!, database: "warehouse" },
      ],
    })

    expect(across.databases).toEqual(["default", "warehouse"])
    expect(across.tablesOn).toEqual({ users: ["default"], posts: ["warehouse"] })
  })
})

describe("statements that ran more than once", () => {
  test("are counted by the SQL they ran", () => {
    expect([...repeats(received(recording)).values()]).toEqual([1, 2])
  })

  test("fold into one, keeping where it first ran and its time in all", () => {
    const kept = folded(recording.statements)
    expect(kept.length).toBe(2)
    expect(kept[1]!.at).toBe(1)
    expect(kept[1]!.one.milliseconds).toBe(3)
  })
})

describe("what a search leaves of a run", () => {
  const run = received(recording)

  test("the statements it matched", () => {
    const { statements, narrowed } = left(run, (one) => one.sql.includes("posts"))
    expect(statements.length).toBe(2)
    expect(narrowed).toBe(true)
  })

  test("all of them, when it matched the recording some other way", () => {
    // A test's name is in the label, never in the SQL.
    const { statements, narrowed } = left(run, (one) => one.sql.includes("test_users"))
    expect(statements.length).toBe(3)
    expect(narrowed).toBe(false)
  })
})
