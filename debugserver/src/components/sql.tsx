/** A statement, laid out over lines and coloured. */

import { Code } from "@/components/code"
import type { Statement } from "@/lib/records"
import { bound, laid } from "@/lib/sql"
import { useStore } from "@/lib/store"

export function readable(one: Statement, values: boolean): string {
  return laid(values ? bound(one.sql, one.parameters) : one.sql)
}

export function Sql({ one }: { one: Statement }) {
  const values = useStore((state) => state.values)
  return <Code code={readable(one, values)} className="sql" />
}
