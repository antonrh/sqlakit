/** The recordings, newest first, one row each. */

import { Search } from "lucide-react"

import { Filters } from "@/components/filters"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import type { Run } from "@/lib/records"
import { kindOf } from "@/lib/sql"
import { ago, clock, ms } from "@/lib/time"
import { TONE } from "@/lib/tone"
import { useStore, type Sort } from "@/lib/store"
import { cn } from "@/lib/utils"

/** The statements of a run as a thin bar, so a row shows its shape. */
function Shape({ run }: { run: Run }) {
  const whole = Math.max(run.milliseconds, 0.001)
  return (
    <span className="flex h-1 w-full gap-px overflow-hidden rounded-full">
      {run.statements.map((one, index) => (
        <i
          key={index}
          className={cn("h-full", TONE[kindOf(one.sql)].fill)}
          style={{ flex: Math.max(one.milliseconds, 0.001) / whole }}
        />
      ))}
    </span>
  )
}

type Props = {
  runs: Run[]
  shown: Run[]
  picked: number | null
  onPick: (id: number) => void
  /** How many databases the page has seen: with one, its name is noise. */
  databases: number
}

export function List({ runs, shown, picked, onPick, databases }: Props) {
  const { search, sort, set, search_ } = useStore((state) => state)
  const apps = new Set(runs.map((run) => run.app)).size

  // With nothing recorded there is nothing to search, and nothing to sort.
  if (!runs.length) return null

  return (
    <>
      <div className="flex flex-col gap-2 border-b p-2">
        <div className="relative">
          <Search className="absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={search}
            onChange={(event) => search_(event.target.value)}
            placeholder="search, or press /"
            data-search
            className="h-8 pl-7 text-sm"
          />
        </div>
        <div className="flex items-center justify-between gap-2">
          <Filters runs={runs} />
          <Select value={sort} onValueChange={(value) => set("sort", value as Sort)}>
            <SelectTrigger size="sm" className="h-8 w-auto gap-1 text-[13px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent align="end">
              <SelectItem value="recent">newest</SelectItem>
              <SelectItem value="slow">slowest</SelectItem>
              <SelectItem value="many">most queries</SelectItem>
              <SelectItem value="repeated">most repeats</SelectItem>
              <SelectItem value="label">label</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {!shown.length && (
          <p className="p-4 text-center text-[13px] text-muted-foreground">
            Nothing matches that search.
          </p>
        )}
        {shown.map((run) => (
          <button
            key={run.id}
            type="button"
            onClick={() => onPick(run.id)}
            className={cn(
              "flex w-full flex-col gap-1 border-b px-3 py-2 text-left hover:bg-accent/60",
              picked === run.id && "bg-accent",
            )}
          >
            <span className="flex items-baseline gap-2">
              {apps > 1 && (
                <span className="shrink-0 font-mono text-[11px] text-teal-700 dark:text-teal-300">
                  {run.app}
                </span>
              )}
              <span className="truncate text-[13px] font-medium">
                {run.label ?? "(no label)"}
              </span>
              {databases > 1 && (
                <span className="shrink-0 font-mono text-[11px] text-muted-foreground">
                  {run.databases.join(" · ")}
                </span>
              )}
              <span className="ml-auto shrink-0 text-[11px] tabular-nums text-muted-foreground">
                {clock(run.at)}
              </span>
            </span>
            <Shape run={run} />
            <span className="flex items-baseline gap-2 text-[11px] text-muted-foreground">
              <span>
                <b className="font-semibold text-foreground">{run.count}</b> queries
              </span>
              <span
                className={cn(
                  "font-semibold text-foreground",
                  run.hot > 0
                    ? "text-rose-600 dark:text-rose-400"
                    : run.slow > 0
                      ? "text-amber-600 dark:text-amber-400"
                      : "",
                )}
              >
                {ms(run.milliseconds)}
              </span>
              {run.slow > 0 && (
                <span
                  className={
                    run.hot > 0
                      ? "text-rose-600 dark:text-rose-400"
                      : "text-amber-600 dark:text-amber-400"
                  }
                >
                  {run.slow} slow
                </span>
              )}
              {run.duplicates > 0 && (
                <span
                  className={
                    run.duplicates > 4
                      ? "text-rose-600 dark:text-rose-400"
                      : "text-amber-600 dark:text-amber-400"
                  }
                >
                  {run.duplicates} repeated
                </span>
              )}
              <span className="ml-auto">{ago(run.at)}</span>
            </span>
          </button>
        ))}
      </div>
    </>
  )
}
