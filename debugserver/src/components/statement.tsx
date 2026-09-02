/** One statement: what it cost on the left, what it was on the right. */

import { Check, Copy } from "lucide-react"
import { useState } from "react"

import { Parameters } from "@/components/parameters"
import { Sql, readable } from "@/components/sql"
import { Trace } from "@/components/trace"
import { kindOf } from "@/lib/sql"
import { ms } from "@/lib/time"
import { TONE } from "@/lib/tone"
import { HOT, SLOW, type Run, type Statement as One } from "@/lib/records"
import { useStore } from "@/lib/store"
import { cn } from "@/lib/utils"

type Props = { one: One; run: Run; index: number; repeats: number; named: boolean }

export function Statement({ one, run, index, repeats, named }: Props) {
  const values = useStore((state) => state.values)
  const [copied, setCopied] = useState(false)
  const took = one.milliseconds

  const copy = () => {
    void navigator.clipboard.writeText(readable(one, values)).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1200)
    })
  }

  return (
    <div
      id={`s-${run.id}-${index}`}
      className={cn("group flex border-t px-4 py-2 text-sm", repeats > 1 && "bg-amber-500/5")}
    >
      <div className="w-28 flex-none pr-3 text-xs tabular-nums text-muted-foreground">
        <span
          className={cn(
            "font-semibold text-foreground",
            took >= HOT
              ? "text-rose-600 dark:text-rose-400"
              : took >= SLOW
                ? "text-amber-600 dark:text-amber-400"
                : "",
          )}
          title={values && repeats > 1 ? `all ${repeats} together` : undefined}
        >
          {ms(took)}
        </span>
        {repeats > 1 && <span> ×{repeats}</span>}
        <span
          className={cn("mt-1 block h-0.5 rounded-full opacity-70", TONE[kindOf(one.sql)].fill)}
          style={{ width: `${Math.max(2, (took / Math.max(run.milliseconds, 0.001)) * 100)}%` }}
        />
        {named && <span className="mt-1 block font-mono text-[11px]">{one.database}</span>}
      </div>
      <div className="min-w-0 flex-1">
        <div className="relative overflow-x-auto rounded-md border bg-muted/40 px-3 py-2">
          <Sql one={one} />
          <button
            type="button"
            onClick={copy}
            title="copy the statement"
            className="absolute right-1.5 top-1.5 hidden rounded-md border bg-background/80
                       p-1 text-muted-foreground backdrop-blur hover:text-foreground
                       group-hover:block"
          >
            {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
          </button>
        </div>
        {!values && <Parameters parameters={one.parameters} />}
        <Trace stack={one.stack} label={run.label} />
      </div>
    </div>
  )
}
