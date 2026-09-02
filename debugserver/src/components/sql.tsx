/** A statement, laid out over lines and coloured. */

import { Code } from "@/components/code"
import type { Statement } from "@/lib/records"
import { bound, formatted, laid } from "@/lib/sql"
import { useStore } from "@/lib/store"

export function readable(one: Statement, values: boolean, pretty = false): string {
  const sql = values ? bound(one.sql, one.parameters) : one.sql
  return pretty ? formatted(sql, one.dialect) : laid(sql)
}

export function Sql({ one }: { one: Statement }) {
  const { values, pretty } = useStore((state) => state)
  return <Code code={readable(one, values, pretty)} className="sql" />
}
