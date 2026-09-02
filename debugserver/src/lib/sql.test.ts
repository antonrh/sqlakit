import { describe, expect, test } from "bun:test"

import { bound, formatted, kindOf, laid, languageOf, tablesIn } from "@/lib/sql"

describe("what a statement is", () => {
  test("the first word", () => {
    expect(kindOf("  SELECT 1")).toBe("select")
    expect(kindOf("INSERT INTO t VALUES (1)")).toBe("insert")
  })

  test("past a template comment", () => {
    expect(kindOf("/* users.sql */ UPDATE users SET a = 1")).toBe("update")
  })

  test("anything else", () => {
    expect(kindOf("PRAGMA foreign_keys")).toBe("other")
  })

  test("the tables it names", () => {
    expect(tablesIn("SELECT * FROM users JOIN posts ON posts.user_id = users.id")).toEqual([
      "users",
      "posts",
    ])
    expect(tablesIn('INSERT INTO "posts" (a) VALUES (1)')).toEqual(["posts"])
  })
})

describe("laying it out", () => {
  test("a line for each clause that leads one", () => {
    expect(laid("SELECT a FROM t JOIN u ON u.id = t.id WHERE a = 1")).toBe(
      ["SELECT a", "FROM t", "JOIN u ON u.id = t.id", "WHERE a = 1"].join("\n"),
    )
  })

  test("the placeholders the drivers use survive it", () => {
    expect(laid("SELECT a FROM t WHERE b = %(name)s")).toContain("%(name)s")
    expect(laid("SELECT a FROM t WHERE b = :name")).toContain(":name")
    expect(laid("SELECT a FROM t WHERE b = $1")).toContain("$1")
  })

  test("a statement with no clauses stays on one line", () => {
    expect(laid("PRAGMA  foreign_keys")).toBe("PRAGMA foreign_keys")
  })

  test("laid out in full, a column to a line", () => {
    expect(formatted("SELECT a, b FROM t WHERE a = 1").split("\n")).toEqual([
      "SELECT",
      "  a,",
      "  b",
      "FROM",
      "  t",
      "WHERE",
      "  a = 1",
    ])
  })

  test("the grammar follows the dialect that ran it", () => {
    expect(languageOf("postgresql")).toBe("postgresql")
    expect(languageOf("oracle")).toBe("plsql")
    expect(languageOf("cockroachdb")).toBe("sql")
    expect(languageOf(undefined)).toBe("sql")
  })

  test("in full, the placeholders survive too", () => {
    expect(formatted("SELECT a FROM t WHERE b = %(name)s")).toContain("%(name)s")
  })

  test("the columns of a select stay together", () => {
    const wide = laid("SELECT a, b, c, d, e FROM t WHERE a = 1")
    expect(wide.split("\n")[0]).toBe("SELECT a, b, c, d, e")
  })
})

describe("the parameters put into the SQL", () => {
  test("positional", () => {
    expect(bound("SELECT * FROM users WHERE id = ?", [5])).toBe(
      "SELECT * FROM users WHERE id = 5",
    )
  })

  test("quotes are doubled, and NULL and booleans are words", () => {
    expect(bound("INSERT INTO t (a, b, c) VALUES (?, ?, ?)", ["O'Hara", null, true])).toBe(
      "INSERT INTO t (a, b, c) VALUES ('O''Hara', NULL, TRUE)",
    )
  })

  test("named, in the three spellings the drivers use", () => {
    expect(bound("SELECT * FROM t WHERE n = :name", { name: "ada" })).toBe(
      "SELECT * FROM t WHERE n = 'ada'",
    )
    expect(bound("SELECT * FROM t WHERE n = %(name)s", { name: "ada" })).toBe(
      "SELECT * FROM t WHERE n = 'ada'",
    )
    expect(bound("SELECT * FROM t WHERE a = $1 OR a = $2", [7, 8])).toBe(
      "SELECT * FROM t WHERE a = 7 OR a = 8",
    )
  })

  test("executemany takes the first row", () => {
    expect(bound("INSERT INTO t (a) VALUES (?)", [[1], [2]])).toBe(
      "INSERT INTO t (a) VALUES (1)",
    )
  })

  test("a `null` first parameter is a value, not a row", () => {
    expect(bound("SELECT * FROM t WHERE a = ? AND b = ?", [null, 2])).toBe(
      "SELECT * FROM t WHERE a = NULL AND b = 2",
    )
  })

  test("no parameters, no change", () => {
    expect(bound("SELECT 1", null)).toBe("SELECT 1")
  })
})
