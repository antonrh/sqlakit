/** A statement, laid out over lines and coloured. */

import { Code } from "@/components/code"
import type { Statement } from "@/lib/records"
import { bound, formatted, laid, type Layout } from "@/lib/sql"
import { useStore } from "@/lib/store"

export function readable(
  one: Statement,
  values: boolean,
  pretty = false,
  how?: Layout,
): string {
  const sql = values ? bound(one.sql, one.parameters) : one.sql
  return pretty ? formatted(sql, one.dialect, how) : laid(sql)
}

export function Sql({ one }: { one: Statement }) {
  const { values, pretty, layout } = useStore((state) => state)
  return <Code code={readable(one, values, pretty, layout)} />
}
