/** What the placeholders stood for, as a table rather than a line of JSON. */

import { ChevronRight } from "lucide-react"
import { useState } from "react"

import { cn } from "@/lib/utils"

type Bound = { name: string; value: unknown }[]

/** The parameters as pairs: named as they are named, positional by their place. */
function pairs(parameters: unknown): Bound | null {
  if (parameters === null || parameters === undefined) return null
  if (Array.isArray(parameters)) {
    const first = parameters[0]
    // `executemany` hands over a row per set of values.
    const many = Array.isArray(first) || (first !== null && typeof first === "object")
    const values = (many ? first : parameters) as unknown[]
    if (!Array.isArray(values)) return pairs(values)
    return values.map((value, index) => ({ name: String(index + 1), value }))
  }
  if (typeof parameters === "object") {
    return Object.entries(parameters as object).map(([name, value]) => ({ name, value }))
  }
  return [{ name: "1", value: parameters }]
}

function said(value: unknown): { text: string; tone: string } {
  if (value === null || value === undefined)
    return { text: "NULL", tone: "text-muted-foreground" }
  if (typeof value === "boolean") {
    return { text: value ? "TRUE" : "FALSE", tone: "text-violet-600 dark:text-violet-300" }
  }
  if (typeof value === "number") {
    return { text: String(value), tone: "text-amber-700 dark:text-amber-300" }
  }
  return { text: String(value), tone: "text-emerald-700 dark:text-emerald-300" }
}

export function Parameters({ parameters }: { parameters: unknown }) {
  const [shown, setShown] = useState(false)
  const bound = pairs(parameters)
  if (!bound?.length) return null

  return (
    <div className="mt-1.5 text-[11px]">
      <button
        type="button"
        onClick={() => setShown(!shown)}
        className="inline-flex items-center gap-0.5 text-muted-foreground hover:text-foreground"
      >
        {bound.length} parameter{bound.length === 1 ? "" : "s"}
        <ChevronRight className={cn("size-3 transition-transform", shown && "rotate-90")} />
      </button>
      {shown && (
        <div className="mt-1 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-0.5 font-mono">
          {bound.map(({ name, value }) => {
            const { text, tone } = said(value)
            return (
              <div key={name} className="contents">
                <span className="text-right text-muted-foreground">{name}</span>
                <span className={cn("truncate", tone)} title={text}>
                  {text}
                </span>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
