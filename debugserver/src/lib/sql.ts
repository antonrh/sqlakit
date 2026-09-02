/** What a statement is, read from the SQL itself. */

import { format } from "sql-formatter"

export const KINDS = ["select", "insert", "update", "delete", "other"] as const

export type Kind = (typeof KINDS)[number]

/** A comment a statement opens with, of either kind. */
const OPENING = /^\s*(?:\/\*[\s\S]*?\*\/|--[^\n]*(?:\n|$))\s*/

/** The first word, past the comments a template writes its name in, or `other`. */
export function kindOf(sql: string): Kind {
  let rest = sql.trimStart()
  while (OPENING.test(rest)) rest = rest.replace(OPENING, "")
  const word = rest.split(/\s+/, 1)[0]?.toLowerCase()
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
  return flat(sql)
    .replace(LEAD, "$1\n")
    .replace(BREAK, "\n$1")
    .split("\n")
    .map(broken)
    .join("\n")
}

/** How long a clause may be before it is broken up as well. */
const WRAP = 100

/**
 * A clause too long to read, broken where it joins one part to the next.
 *
 * The columns a mapper selects fit on a line or two. A report written by hand
 * has thirty of them, and the clause alone is a paragraph, so its commas and
 * its `AND`s become lines. What is inside brackets stays where it is.
 */
function broken(clause: string): string {
  if (clause.length <= WRAP) return clause
  const parts: string[] = []
  let depth = 0
  let quoted = false
  let held = ""
  for (const [at, letter] of [...clause].entries()) {
    if (letter === "'") quoted = !quoted
    if (!quoted && letter === "(") depth += 1
    if (!quoted && letter === ")") depth -= 1
    held += letter
    const joins = !quoted && depth === 0 && (letter === "," || /\s(and|or)$/i.test(held))
    if (joins && at < clause.length - 1) {
      parts.push(held)
      held = ""
    }
  }
  parts.push(held)
  if (parts.length < 2) return clause
  return parts
    .map((part, at) => (at === 0 ? part.trim() : `  ${part.trim()}`))
    .filter(Boolean)
    .join("\n")
}

/** A comment a statement opens with, which a template writes its name in. */
const LEAD = /^(\/\*[\s\S]*?\*\/)[ \t]*\n?/

/**
 * The statement on one line, except where a comment ends one.
 *
 * A template names itself in a comment, and joining a `--` line to the next
 * would comment the statement out. A block comment keeps its line because
 * that is where the reader looks for the name.
 */
export function flat(sql: string): string {
  return sql
    .split("\n")
    .map((line) => line.replace(/\s+/g, " ").trim())
    .filter(Boolean)
    .reduce((held, line) => held + (ENDED.test(held) ? "\n" : " ") + line)
}

/** Whether the text so far ends in a comment, of either kind. */
const ENDED = /--[^\n]*$|\*\/$/

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
