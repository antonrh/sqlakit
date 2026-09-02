/** What a statement is, read from the SQL itself. */

import { format } from "sql-formatter"

export const KINDS = ["select", "insert", "update", "delete", "other"] as const

export type Kind = (typeof KINDS)[number]

/** The first word, past a template comment, or `other`. */
export function kindOf(sql: string): Kind {
  const word = sql
    .replace(/^\s*(\/\*[\s\S]*?\*\/)?\s*/, "")
    .split(/\s+/, 1)[0]
    ?.toLowerCase()
  return KINDS.includes(word as Kind) && word !== "other" ? (word as Kind) : "other"
}

const TABLE = /\b(?:FROM|JOIN|INTO|UPDATE)\s+("?[a-zA-Z_][\w.]*"?)/gi

/** The tables a statement names, in the order it names them. */
export function tablesIn(sql: string): string[] {
  return [...sql.matchAll(TABLE)].map((found) => found[1]!.replace(/"/g, ""))
}

const BREAK = new RegExp(
  "\\s+(FROM|WHERE|GROUP BY|ORDER BY|HAVING|LIMIT|OFFSET|VALUES|SET|RETURNING" +
    "|UNION ALL|UNION|ON CONFLICT" +
    "|(?:LEFT |RIGHT |FULL |INNER |CROSS )?(?:OUTER )?JOIN)\\b",
  "gi",
)

/**
 * The statement over several lines, broken before the clauses that lead one.
 *
 * A full formatter puts every column of a `SELECT` on a line of its own, which
 * for the forty a mapper writes is a screen of names. The clauses are what a
 * reader is looking for, so those are what the lines are.
 */
export function laid(sql: string): string {
  return flat(sql).replace(BREAK, "\n$1")
}

/**
 * The statement on one line, except where a line comment ends one.
 *
 * A template names itself in a `--` comment, and joining that line to the next
 * would comment the statement out.
 */
export function flat(sql: string): string {
  return sql
    .split("\n")
    .map((line) => line.replace(/\s+/g, " ").trim())
    .filter(Boolean)
    .reduce((held, line) => held + (/--[^\n]*$/.test(held) ? "\n" : " ") + line)
}

/**
 * The statement as a formatter lays it out, a column to a line.
 *
 * Roomier than the clauses alone, and worth it for a statement you are
 * reading closely. What it cannot parse comes back on one line.
 */
const LANGUAGES: Record<string, string> = {
  postgresql: "postgresql",
  mysql: "mysql",
  mariadb: "mariadb",
  sqlite: "sqlite",
  oracle: "plsql",
  mssql: "transactsql",
}

/** The grammar to read a statement with, as SQLAlchemy names the dialect. */
export const languageOf = (dialect?: string): string => LANGUAGES[dialect ?? ""] ?? "sql"

export type Layout = {
  indent: "compact" | "standard" | "tabularLeft"
  keywords: "upper" | "preserve" | "lower"
  width: number
}

export const LAYOUT: Layout = { indent: "compact", keywords: "upper", width: 72 }

/** The statement as the reader asked to see it. */
export function shown(sql: string, dialect?: string, how: Layout = LAYOUT): string {
  return how.indent === "compact" ? laid(sql) : formatted(sql, dialect, how)
}

export function formatted(sql: string, dialect?: string, how: Layout = LAYOUT): string {
  const one = flat(sql)
  try {
    return format(one, {
      language: languageOf(dialect) as never,
      keywordCase: how.keywords,
      indentStyle: how.indent === "compact" ? "standard" : how.indent,
      expressionWidth: how.width,
      paramTypes: { named: [":"], numbered: ["$"], custom: [{ regex: "%\\(\\w+\\)s" }] },
    })
  } catch {
    return one
  }
}

function literal(value: unknown): string {
  if (value === null || value === undefined) return "NULL"
  if (typeof value === "boolean") return value ? "TRUE" : "FALSE"
  if (typeof value === "number") return String(value)
  return `'${String(value).replace(/'/g, "''")}'`
}

/**
 * The SQL as it reads with the parameters in it.
 *
 * What ran is the statement with the placeholders. This is for pasting into a
 * client, and it covers the four the drivers use: `?`, `$1`, `:name`, `%(name)s`.
 */
export function bound(sql: string, parameters: unknown): string {
  if (parameters === null || parameters === undefined) return sql
  const first = Array.isArray(parameters) ? parameters[0] : null
  const many = Array.isArray(first) || (first !== null && typeof first === "object")
  const values = many ? (parameters as unknown[])[0] : parameters
  if (values === null || values === undefined) return sql
  if (Array.isArray(values)) {
    let next = 0
    return sql.replace(/\?|\$(\d+)/g, (_match, number) =>
      literal(values[number ? Number(number) - 1 : next++]),
    )
  }
  const named = values as Record<string, unknown>
  return sql.replace(/:([a-zA-Z_]\w*)|%\((\w+)\)s/g, (match, colon, percent) => {
    const name = colon ?? percent
    return name in named ? literal(named[name]) : match
  })
}
