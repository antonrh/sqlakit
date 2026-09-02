/** The recording the reader picked: what it ran, in the order it ran it. */

import { ChevronDown, Code2, FoldVertical, IndentIncrease, Variable } from "lucide-react"

import { Statement } from "@/components/statement"
import { Waterfall } from "@/components/waterfall"
import { Badge } from "@/components/ui/badge"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
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

/**
 * A pressed toggle reads as pressed.
 *
 * By `aria`, not by `data-state`: these sit inside a tooltip trigger, and it
 * puts its own state, `closed`, on the button it wraps.
 */
const PRESSED = cn(
  "aria-checked:border-teal-600 aria-checked:bg-teal-500/25",
  "aria-checked:text-teal-700 dark:aria-checked:text-teal-200",
  "aria-pressed:border-teal-600 aria-pressed:bg-teal-500/25",
  "aria-pressed:text-teal-700 dark:aria-pressed:text-teal-200",
)

function Choices({
  title,
  value,
  among,
  onPick,
}: {
  title: string
  value: string
  among: [string, string][]
  onPick: (one: string) => void
}) {
  return (
    <div className="mb-2 last:mb-0">
      <div className="px-1 pb-1 text-[11px] uppercase tracking-wider text-muted-foreground">
        {title}
      </div>
      <div className="flex flex-wrap gap-1">
        {among.map(([one, said]) => (
          <button
            key={one}
            type="button"
            onClick={() => onPick(one)}
            className={cn(
              "rounded-md border px-2 py-0.5 text-[12px] text-muted-foreground",
              "hover:bg-accent hover:text-foreground",
              value === one &&
                "border-teal-600 bg-teal-500/20 text-teal-700 dark:text-teal-200",
            )}
          >
            {said}
          </button>
        ))}
      </div>
    </div>
  )
}

export function Detail({ run, narrow }: { run: Run; narrow: ((one: One) => boolean) | null }) {
  const { fold, values, pretty, layout, set, toggleTag } = useStore((state) => state)
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
                    <ToggleGroupItem value="raw" className={cn("size-7 px-0", PRESSED)}>
                      <Code2 className="size-3.5" />
                    </ToggleGroupItem>
                  </TooltipTrigger>
                  <TooltipContent>the statement as the database ran it</TooltipContent>
                </Tooltip>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <ToggleGroupItem value="values" className={cn("size-7 px-0", PRESSED)}>
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
                    className={cn("size-7 px-0", PRESSED)}
                  >
                    <FoldVertical className="size-3.5" />
                  </Toggle>
                </TooltipTrigger>
                <TooltipContent>
                  a statement that ran more than once, listed once
                </TooltipContent>
              </Tooltip>
              <div
                className={cn(
                  "flex items-center rounded-md border",
                  pretty && "border-teal-600 bg-teal-500/25 text-teal-700 dark:text-teal-200",
                )}
              >
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Toggle
                      size="sm"
                      variant="default"
                      pressed={pretty}
                      onPressedChange={(on) => set("pretty", on)}
                      className="size-7 rounded-r-none px-0 hover:bg-transparent
                                 aria-pressed:bg-transparent"
                    >
                      <IndentIncrease className="size-3.5" />
                    </Toggle>
                  </TooltipTrigger>
                  <TooltipContent>laid out in full, a column to a line</TooltipContent>
                </Tooltip>
                <Popover>
                  <PopoverTrigger
                    title="how it is laid out"
                    className="rounded-r-md py-1 pl-0 pr-1 opacity-70 hover:opacity-100"
                  >
                    <ChevronDown className="size-3" />
                  </PopoverTrigger>
                  <PopoverContent align="end" className="w-56 p-2 text-[13px]">
                    <Choices
                      title="indent"
                      value={layout.indent}
                      among={[
                        ["standard", "standard"],
                        ["tabularLeft", "tabular"],
                      ]}
                      onPick={(one) => set("layout", { ...layout, indent: one as never })}
                    />
                    <Choices
                      title="keywords"
                      value={layout.keywords}
                      among={[
                        ["upper", "UPPER"],
                        ["lower", "lower"],
                        ["preserve", "as they came"],
                      ]}
                      onPick={(one) => set("layout", { ...layout, keywords: one as never })}
                    />
                    <Choices
                      title="width"
                      value={String(layout.width)}
                      among={[
                        ["50", "50"],
                        ["72", "72"],
                        ["100", "100"],
                      ]}
                      onPick={(one) => set("layout", { ...layout, width: Number(one) })}
                    />
                  </PopoverContent>
                </Popover>
              </div>
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
