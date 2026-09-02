/**
 * What the search understands.
 *
 * `app:web queries:>5 users` reads as three terms. A term about a recording
 * says which cards are listed. A term about a statement also says which
 * statements a card lists, so a search for a table leaves the queries that
 * touched it.
 */

import type { Run, Statement } from "@/lib/records"
import { kindOf, tablesIn } from "@/lib/sql"

const OF_RUN: Record<string, (run: Run) => string> = {
  app: (run) => run.app,
  tag: (run) => run.tags.join(" "),
  label: (run) => run.label ?? "",
  sql: (run) => run.statements.map((one) => one.sql).join(" "),
  db: (run) => run.statements.map((one) => one.database).join(" "),
  table: (run) => run.tables.join(" "),
  kind: (run) => Object.keys(run.kinds).join(" "),
  trace: (run) => run.statements.flatMap((one) => one.stack ?? []).join(" "),
}

const COUNTED: Record<string, (run: Run) => number> = {
  queries: (run) => run.count,
  ms: (run) => run.milliseconds,
  repeated: (run) => run.duplicates,
}

const OF_STATEMENT: Record<string, (one: Statement, wanted: string) => boolean> = {
  table: (one, wanted) =>
    tablesIn(one.sql).some((table) => table.toLowerCase().includes(wanted)),
  kind: (one, wanted) => kindOf(one.sql).includes(wanted),
  sql: (one, wanted) => one.sql.toLowerCase().includes(wanted),
  db: (one, wanted) => one.database.toLowerCase().includes(wanted),
  trace: (one, wanted) => (one.stack ?? []).join(" ").toLowerCase().includes(wanted),
}

export const FIELDS = [
  ["label:", "users"],
  ["sql:", "insert"],
  ["table:", "users"],
  ["kind:", "delete"],
  ["db:", "warehouse"],
  ["trace:", "views.py"],
  ["queries:", ">5"],
  ["ms:", ">50"],
  ["repeated:", ">0"],
] as const

const TERM = /(\w+):(>=|<=|>|<)?("[^"]*"|\S+)/g

const compare = (operator: string | undefined, wanted: number) =>
  ({
    ">": (had: number) => had > wanted,
    "<": (had: number) => had < wanted,
    ">=": (had: number) => had >= wanted,
    "<=": (had: number) => had <= wanted,
  })[operator ?? ">="]!

/**
 * The recordings a search leaves.
 *
 * Two terms of one field are read as either, so picking a second table in the
 * filters widens the list. Terms of different fields are read as both.
 */
export function asQuery(text: string): (run: Run) => boolean {
  const terms = new Map<string, ((run: Run) => boolean)[]>()
  const under = (field: string, test: (run: Run) => boolean) =>
    terms.set(field, [...(terms.get(field) ?? []), test])
  const words = text
    .replace(TERM, (match, field: string, operator: string, value: string) => {
      const bare = value.replace(/^"|"$/g, "")
      if (field in OF_RUN) {
        under(field, (run) => OF_RUN[field]!(run).toLowerCase().includes(bare.toLowerCase()))
        return ""
      }
      if (field in COUNTED) {
        const wanted = Number(bare)
        const test = compare(operator, wanted)
        if (!Number.isNaN(wanted)) under(field, (run) => test(COUNTED[field]!(run)))
        return ""
      }
      return match
    })
    .trim()
    .toLowerCase()

  if (words) {
    under(
      "",
      (run) =>
        (run.label ?? "").toLowerCase().includes(words) ||
        run.statements.some((one) => one.sql.toLowerCase().includes(words)),
    )
  }
  return (run) => [...terms.values()].every((group) => group.some((test) => test(run)))
}

/** The statements a search leaves of a card, or null when it says nothing
 * about statements. */
export function asNarrowing(text: string): ((one: Statement) => boolean) | null {
  const tests = new Map<string, ((one: Statement) => boolean)[]>()
  const under = (field: string, test: (one: Statement) => boolean) =>
    tests.set(field, [...(tests.get(field) ?? []), test])
  const words = text
    .replace(TERM, (match, field: string, _operator: string, value: string) => {
      const bare = value.replace(/^"|"$/g, "").toLowerCase()
      if (field in OF_STATEMENT) {
        under(field, (one) => OF_STATEMENT[field]!(one, bare))
        return ""
      }
      return field in OF_RUN || field in COUNTED ? "" : match
    })
    .trim()
    .toLowerCase()

  if (words) under("", (one) => one.sql.toLowerCase().includes(words))
  if (!tests.size) return null
  return (one) => [...tests.values()].every((group) => group.some((test) => test(one)))
}

/** The terms of a search, as words. */
export const termsOf = (text: string): string[] => text.split(/\s+/).filter(Boolean)

/** The search with a term added, or taken back out. */
export function withTerm(text: string, term: string): string {
  const words = termsOf(text)
  return (words.includes(term) ? words.filter((word) => word !== term) : [...words, term]).join(
    " ",
  )
}
