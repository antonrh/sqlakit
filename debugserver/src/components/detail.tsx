/** The recording the reader picked: what it ran, in the order it ran it. */

import { Code2, FoldVertical, Variable } from "lucide-react"

import { Statement } from "@/components/statement"
import { Waterfall } from "@/components/waterfall"
import { Badge } from "@/components/ui/badge"
import { Toggle } from "@/components/ui/toggle"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { folded, repeats, type Run, type Statement as One } from "@/lib/records"
import type { Kind } from "@/lib/sql"
import { ago, clock, exactly, ms } from "@/lib/time"
import { TONE } from "@/lib/tone"
import { useStore } from "@/lib/store"
import { cn } from "@/lib/utils"

const REPEATS = 3

export function Detail({ run, narrow }: { run: Run; narrow: ((one: One) => boolean) | null }) {
  const { fold, values, set, toggleTag } = useStore((state) => state)
  const seen = repeats(run)
  const matching = narrow ? run.statements.filter(narrow) : run.statements
  const listed = fold
    ? folded(matching)
    : matching.map((one) => ({ one, at: run.statements.indexOf(one) }))

  const show = (index: number) => {
    const row = document.getElementById(`s-${run.id}-${index}`)
    row?.scrollIntoView({ block: "center", behavior: "smooth" })
    row?.animate([{ opacity: 0.35 }, { opacity: 1 }], { duration: 600 })
  }

  return (
    <>
      <div className="sticky top-0 z-10 border-b bg-background/90 backdrop-blur">
        <div className="flex flex-wrap items-center gap-2 px-4 pb-2 pt-3">
          <Badge
            variant="outline"
            className="font-mono text-[11px] text-teal-700 dark:text-teal-300"
          >
            {run.app}
          </Badge>
          <h2 className="text-sm font-semibold">{run.label ?? "(no label)"}</h2>
          {run.tags.map((tag) => (
            <button
              key={tag}
              type="button"
              onClick={() => toggleTag(tag)}
              className="text-xs text-muted-foreground hover:text-foreground"
            >
              #{tag}
            </button>
          ))}
          <span
            className="ml-auto text-xs tabular-nums text-muted-foreground"
            title={exactly(run.at)}
          >
            {clock(run.at)} · {ago(run.at)}
          </span>
        </div>

        <Waterfall run={run} narrow={narrow} onPick={show} />

        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 pb-2 text-xs">
          <span>
            <b className="font-semibold">{run.count}</b>{" "}
            <span className="text-muted-foreground">queries</span>
          </span>
          <span className="font-semibold">{ms(run.milliseconds)}</span>
          {Object.entries(run.kinds).map(([kind, many]) => (
            <span
              key={kind}
              className={cn("flex items-baseline gap-1.5", TONE[kind as Kind].text)}
            >
              <span className={cn("size-1.5 rounded-xs", TONE[kind as Kind].fill)} />
              {many} {kind}
            </span>
          ))}
          {narrow && matching.length < run.count && (
            <span className="text-teal-700 dark:text-teal-300">
              {matching.length} of {run.count} match
            </span>
          )}
          {run.duplicates >= REPEATS && !fold && (
            <span className="text-amber-600 dark:text-amber-400">
              {run.duplicates} repeat{" "}
              <button
                type="button"
                onClick={() => set("fold", true)}
                className="underline underline-offset-2 hover:text-foreground"
              >
                fold them
              </button>
            </span>
          )}

          <TooltipProvider delayDuration={300}>
            <span className="ml-auto flex items-center gap-1">
              <ToggleGroup
                type="single"
                size="sm"
                variant="outline"
                value={values ? "values" : "raw"}
                onValueChange={(picked) => picked && set("values", picked === "values")}
              >
                <Tooltip>
                  <TooltipTrigger asChild>
                    <ToggleGroupItem value="raw" className="size-7 px-0">
                      <Code2 className="size-3.5" />
                    </ToggleGroupItem>
                  </TooltipTrigger>
                  <TooltipContent>the statement as the database ran it</TooltipContent>
                </Tooltip>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <ToggleGroupItem value="values" className="size-7 px-0">
                      <Variable className="size-3.5" />
                    </ToggleGroupItem>
                  </TooltipTrigger>
                  <TooltipContent>the parameters put into the SQL</TooltipContent>
                </Tooltip>
              </ToggleGroup>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Toggle
                    size="sm"
                    variant="outline"
                    pressed={fold}
                    onPressedChange={(on) => set("fold", on)}
                    className="size-7 px-0"
                  >
                    <FoldVertical className="size-3.5" />
                  </Toggle>
                </TooltipTrigger>
                <TooltipContent>
                  a statement that ran more than once, listed once
                </TooltipContent>
              </Tooltip>
            </span>
          </TooltipProvider>
        </div>
      </div>

      {listed.map(({ one, at }) => (
        <Statement key={at} one={one} run={run} index={at} repeats={seen.get(one.sql) ?? 1} />
      ))}
    </>
  )
}
