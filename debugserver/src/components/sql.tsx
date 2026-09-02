/** A statement, laid out over lines and coloured. */

import { Code } from "@/components/code"
import type { Statement } from "@/lib/records"
import { bound, shown, type Layout } from "@/lib/sql"
import { useStore } from "@/lib/store"

export function readable(one: Statement, values: boolean, how?: Layout): string {
  return shown(values ? bound(one.sql, one.parameters) : one.sql, one.dialect, how)
}

export function Sql({ one }: { one: Statement }) {
  const { values, layout } = useStore((state) => state)
  return <Code code={readable(one, values, layout)} />
}
