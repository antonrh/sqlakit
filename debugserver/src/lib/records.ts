/** What the server sends, and what the page adds to it. */

import { kindOf, tablesIn, type Kind } from "@/lib/sql"

export type Statement = {
  sql: string
  parameters: unknown
  milliseconds: number
  database: string
  dialect?: string
  stack: string[]
}

export type Recording = {
  app: string
  tags: string[]
  label: string | null
  count: number
  at: number
  milliseconds: number
  duplicates: number
  statements: Statement[]
}

export type Run = Recording & {
  id: number
  key: string
  kinds: Partial<Record<Kind, number>>
  tables: string[]
  databases: number
}

let counter = 0

/** Return the recording with what the page sorts, counts and groups by. */
export function received(recording: Recording): Run {
  const kinds: Partial<Record<Kind, number>> = {}
  const tables = new Set<string>()
  for (const one of recording.statements) {
    const kind = kindOf(one.sql)
    kinds[kind] = (kinds[kind] ?? 0) + 1
    for (const table of tablesIn(one.sql)) tables.add(table)
  }
  return {
    ...recording,
    id: ++counter,
    at: recording.at || Date.now(),
    key: `${recording.app} · ${recording.label ?? "(no label)"}`,
    kinds,
    tables: [...tables],
    databases: new Set(recording.statements.map((one) => one.database)).size,
  }
}

/**
 * The statements a search leaves of a run.
 *
 * A word that matched the label rather than any SQL leaves every statement:
 * the recording was found some other way, and there is nothing to narrow.
 */
export function left(
  run: Run,
  narrow: ((one: Statement) => boolean) | null,
): { statements: Statement[]; narrowed: boolean } {
  if (!narrow) return { statements: run.statements, narrowed: false }
  const kept = run.statements.filter(narrow)
  return kept.length
    ? { statements: kept, narrowed: kept.length < run.statements.length }
    : { statements: run.statements, narrowed: false }
}

/** How many times each statement of a run ran, by the SQL it ran. */
export function repeats(run: Run): Map<string, number> {
  const seen = new Map<string, number>()
  for (const one of run.statements) seen.set(one.sql, (seen.get(one.sql) ?? 0) + 1)
  return seen
}

/** A repeated statement once, keeping where it first ran and its time in all. */
export function folded(statements: Statement[]): { one: Statement; at: number }[] {
  const kept: { one: Statement; at: number }[] = []
  const where = new Map<string, number>()
  statements.forEach((one, index) => {
    const already = where.get(one.sql)
    if (already !== undefined) {
      kept[already]!.one = {
        ...kept[already]!.one,
        milliseconds: kept[already]!.one.milliseconds + one.milliseconds,
      }
      return
    }
    where.set(one.sql, kept.length)
    kept.push({ one, at: index })
  })
  return kept
}
