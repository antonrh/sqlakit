import { describe, expect, test } from "bun:test"

import { coloured } from "@/lib/highlight"

const parts = (html: string) =>
  [...html.matchAll(/<span class="tok-(\w+)">(.*?)<\/span>/g)].map((one) => [one[1], one[2]])

describe("colouring SQL", () => {
  test("the clauses, the ones a grammar tends to forget among them", () => {
    const said = parts(coloured("SELECT a FROM t LIMIT ? OFFSET ? RETURNING id"))
    const words = said.filter(([kind]) => kind === "keyword").map(([, text]) => text)
    expect(words).toEqual(["SELECT", "FROM", "LIMIT", "OFFSET", "RETURNING"])
  })

  test("a parameter, in each spelling a driver uses", () => {
    for (const one of ["?", "$1", ":name", "%(name)s"]) {
      expect(parts(coloured(`SELECT a FROM t WHERE b = ${one}`))).toContainEqual([
        "parameter",
        one,
      ])
    }
  })

  test("strings, numbers and the comment a template leaves", () => {
    const said = parts(coloured("/* users.sql */ SELECT 'ada', 42 FROM t"))
    expect(said).toContainEqual(["comment", "/* users.sql */"])
    expect(said).toContainEqual(["string", "'ada'"])
    expect(said).toContainEqual(["number", "42"])
  })

  test("what is in the SQL cannot open a tag", () => {
    expect(coloured("SELECT a FROM t WHERE b < 1 AND c > 2")).not.toContain("<script")
    expect(coloured("SELECT '<b>'")).toContain("&lt;b&gt;")
  })
})

describe("colouring Python", () => {
  test("the words the block on the empty page is made of", () => {
    const said = parts(coloured('with db.recording("GET /users"):', "python"))
    expect(said).toContainEqual(["keyword", "with"])
    expect(said).toContainEqual(["string", '"GET /users"'])
  })
})
