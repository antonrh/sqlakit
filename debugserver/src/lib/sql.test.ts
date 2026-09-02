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

describe("a statement whose template named itself in a comment", () => {
  test("is the kind of statement past the comment, of either kind", () => {
    expect(kindOf("/* this is comment */ select 1")).toBe("select")
    expect(kindOf("-- users.sql\nSELECT count(*) FROM users")).toBe("select")
    expect(kindOf("/* a */\n-- b\nINSERT INTO users VALUES (1)")).toBe("insert")
  })

  const sql = "-- users.sql\nSELECT users.id\nFROM users\nWHERE users.id = :id"

  test("keeps the comment on a line of its own", () => {
    expect(laid(sql).split("\n")).toEqual([
      "-- users.sql",
      "SELECT users.id",
      "FROM users",
      "WHERE users.id = :id",
    ])
  })

  test("is laid out by the formatter rather than commented out", () => {
    const held = formatted(sql, "postgresql")

    expect(held.split("\n")[0]).toBe("-- users.sql")
    expect(held).toContain("FROM\n  users")
  })

  test("a block comment keeps a line of its own, wherever it arrived", () => {
    expect(laid("/* users.sql */ SELECT 1 FROM users")).toBe(
      "/* users.sql */\nSELECT 1\nFROM users",
    )
    expect(laid("/* users.sql */\nSELECT 1 FROM users")).toBe(
      "/* users.sql */\nSELECT 1\nFROM users",
    )
  })

  test("a comment further in stays where it is", () => {
    expect(laid("SELECT /* mid */ 1 FROM users")).toBe("SELECT /* mid */ 1\nFROM users")
    expect(laid("SELECT 1 /* trailing */")).toBe("SELECT 1 /* trailing */")
  })
})

describe("a clause too long to read", () => {
  const columns = Array.from({ length: 20 }, (_, at) => `column_number_${at}`).join(", ")

  test("breaks at the commas of the clause, not at those inside brackets", () => {
    const lines = laid(`SELECT ${columns}, count(a, b) FROM users`).split("\n")

    expect(lines[0]).toBe("SELECT column_number_0,")
    expect(lines[1]).toBe("  column_number_1,")
    expect(lines.at(-2)).toBe("  count(a, b)")
    expect(lines.at(-1)).toBe("FROM users")
  })

  test("breaks where one condition joins the next", () => {
    const conditions = Array.from(
      { length: 8 },
      (_, at) => `users.column_number_${at} = ${at}`,
    ).join(" AND ")
    const lines = laid(`SELECT id FROM users WHERE ${conditions}`).split("\n")

    expect(lines[2]).toBe("WHERE users.column_number_0 = 0 AND")
    expect(lines[3]).toBe("  users.column_number_1 = 1 AND")
  })

  test("leaves a comma inside a value where it is", () => {
    const long = `WHERE city in ('Rome, Italy', 'Bath, England') AND plan = 'free' AND ${"x".repeat(60)} = 1`

    expect(laid(`SELECT id FROM users ${long}`).split("\n")[2]).toBe(
      "WHERE city in ('Rome, Italy', 'Bath, England') AND",
    )
  })

  test("a statement a mapper wrote is left alone", () => {
    expect(laid("SELECT users.id AS users_id FROM users WHERE users.id = ?")).toBe(
      "SELECT users.id AS users_id\nFROM users\nWHERE users.id = ?",
    )
  })
})
