import { describe, expect, test } from "bun:test"

import { received, type Recording, type Run } from "@/lib/records"
import { asNarrowing, asQuery, withTerm } from "@/lib/search"

const recording = (over: Partial<Recording> = {}): Recording => ({
  app: "web",
  tags: ["api"],
  label: "GET /users",
  count: 3,
  at: Date.now(),
  milliseconds: 12,
  duplicates: 2,
  statements: [
    {
      sql: "SELECT id FROM users",
      parameters: null,
      milliseconds: 4,
      database: "default",
      stack: ["/app/views.py:31 in list_users"],
    },
    {
      sql: "INSERT INTO posts (a) VALUES (1)",
      parameters: null,
      milliseconds: 4,
      database: "default",
      stack: [],
    },
    {
      sql: "SELECT id FROM posts",
      parameters: null,
      milliseconds: 4,
      database: "default",
      stack: [],
    },
  ],
  ...over,
})

const run = (over: Partial<Recording> = {}) => received(recording(over))

describe("which recordings a search leaves", () => {
  const keeps = (query: string, over: Partial<Recording> = {}) => asQuery(query)(run(over))

  test("a field about the recording", () => {
    expect(keeps("app:web")).toBe(true)
    expect(keeps("app:worker")).toBe(false)
    expect(keeps("tag:api")).toBe(true)
    expect(keeps('label:"GET /users"')).toBe(true)
  })

  test("a number, with or without an operator", () => {
    expect(keeps("queries:>2")).toBe(true)
    expect(keeps("queries:>9")).toBe(false)
    expect(keeps("ms:<20 repeated:>0")).toBe(true)
  })

  test("what the statements say", () => {
    expect(keeps("table:posts")).toBe(true)
    expect(keeps("table:orders")).toBe(false)
    expect(keeps("kind:insert")).toBe(true)
    expect(keeps("trace:views.py")).toBe(true)
  })

  test("anything else matches the label or the SQL", () => {
    expect(keeps("users")).toBe(true)
    expect(keeps("nowhere")).toBe(false)
  })
})

describe("which statements a search leaves", () => {
  const left = (query: string) => {
    const narrow = asNarrowing(query)
    const one = run()
    return narrow ? one.statements.filter(narrow).length : one.statements.length
  }

  test("a field about a statement narrows them", () => {
    expect(left("table:posts")).toBe(2)
    expect(left("kind:insert")).toBe(1)
    expect(left("table:posts kind:select")).toBe(1)
  })

  test("a field about the recording leaves them alone", () => {
    expect(asNarrowing("app:web")).toBeNull()
    expect(left("app:web")).toBe(3)
  })
})

describe("a term the filters write into the search", () => {
  test("goes in, and comes back out", () => {
    expect(withTerm("", "kind:insert")).toBe("kind:insert")
    expect(withTerm("app:web kind:insert", "kind:insert")).toBe("app:web")
  })
})

describe("two terms of one field", () => {
  const of = (label: string, table: string, app = "web"): Run =>
    received({
      app,
      tags: [],
      label,
      count: 1,
      at: 0,
      milliseconds: 1,
      duplicates: 0,
      statements: [
        {
          sql: `SELECT id FROM ${table}`,
          parameters: null,
          milliseconds: 1,
          database: "default",
          stack: [],
        },
      ],
    })

  const runs = [of("GET /users", "users"), of("GET /posts", "posts"), of("GET /teams", "teams")]

  test("are read as either of them", () => {
    const left = runs.filter(asQuery("table:users table:posts"))

    expect(left.map((run) => run.label)).toEqual(["GET /users", "GET /posts"])
  })

  test("and terms of two fields as both", () => {
    const left = runs.filter(asQuery("table:users table:posts app:web queries:>0"))

    expect(left).toHaveLength(2)
    expect(runs.filter(asQuery("table:users app:worker"))).toEqual([])
  })

  test("narrow the statements to either as well", () => {
    const narrow = asNarrowing("table:users table:posts")
    const statements = [...runs[0]!.statements, ...runs[2]!.statements]

    expect(statements.filter(narrow!).map((one) => one.sql)).toEqual(["SELECT id FROM users"])
  })
})
