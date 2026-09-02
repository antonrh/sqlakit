/** What there is to narrow by, one input for each of them, behind one button. */

import { ChevronDown, Filter, X } from "lucide-react"
import { Command as CommandPrimitive } from "cmdk"
import { useRef, useState } from "react"

import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandItem,
  CommandList,
} from "@/components/ui/command"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { asQuery, termsOf, withTerm } from "@/lib/search"
import type { Run } from "@/lib/records"
import type { Kind } from "@/lib/sql"
import { TONE } from "@/lib/tone"
import { useStore } from "@/lib/store"
import { cn } from "@/lib/utils"

const WORTH = [
  ["repeated:>0", "repeats itself"],
  ["ms:>50", "over 50 ms"],
  ["queries:>10", "over 10 queries"],
] as const

/** One choice of an input: what it says, what it costs, and where it lives. */
type Choice = {
  value: string
  name: string
  count: number
  under?: string
  dot?: Kind
}

function tally(runs: Run[], of: (run: Run) => string[]): [string, number][] {
  const counted = new Map<string, number>()
  for (const run of runs) {
    for (const one of of(run)) counted.set(one, (counted.get(one) ?? 0) + 1)
  }
  return [...counted].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
}

/** The tables of each database, counted, for a page that has more than one. */
function tablesOn(runs: Run[]): Map<string, Map<string, number>> {
  const touched = new Map<string, Map<string, number>>()
  for (const run of runs) {
    for (const [table, on] of Object.entries(run.tablesOn)) {
      for (const name of on) {
        const held = touched.get(name) ?? new Map<string, number>()
        held.set(table, (held.get(table) ?? 0) + 1)
        touched.set(name, held)
      }
    }
  }
  return touched
}

type FieldProps = {
  title: string
  choices: Choice[]
  chosen: string[]
  onPick: (value: string) => void
  mono?: boolean
}

/**
 * One field to narrow by: what is picked sits in the box, the rest is under it.
 *
 * The box is an input, so a field with two hundred tables is typed at rather
 * than scrolled through. `cmdk` does the narrowing and the arrow keys.
 */
function Field({ title, choices, chosen, onPick, mono }: FieldProps) {
  const [look, setLook] = useState("")
  const [open, setOpen] = useState(false)
  const box = useRef<HTMLInputElement>(null)
  if (choices.length < 2) return null

  const named = (value: string) => choices.find((one) => one.value === value)?.name ?? value
  const groups = [...new Set(choices.map((one) => one.under ?? ""))]

  const take = (value: string) => {
    onPick(value)
    setLook("")
    box.current?.focus()
  }

  return (
    <div>
      <div
        className="px-0.5 pb-1 text-[11px] font-semibold uppercase tracking-wider
                   text-muted-foreground/70"
      >
        {title}
      </div>
      <Command loop className="overflow-visible bg-transparent">
        <div
          onClick={() => box.current?.focus()}
          className={cn(
            "flex flex-wrap items-center gap-1 rounded-md border px-1.5 py-1",
            "cursor-text focus-within:border-teal-600/50",
          )}
        >
          {chosen.map((value) => (
            <span
              key={value}
              className={cn(
                "flex max-w-full items-center gap-1 rounded-sm bg-teal-500/15 py-0.5 pl-1.5 pr-1",
                "text-[11px] text-teal-700 dark:text-teal-300",
                mono && "font-mono",
              )}
            >
              <span className="truncate">{named(value)}</span>
              <button
                type="button"
                title="drop this one"
                onClick={(event) => {
                  event.stopPropagation()
                  onPick(value)
                }}
              >
                <X className="size-3 opacity-70 hover:opacity-100" />
              </button>
            </span>
          ))}
          <CommandPrimitive.Input
            ref={box}
            value={look}
            onValueChange={setLook}
            onFocus={() => setOpen(true)}
            onBlur={() => setTimeout(() => setOpen(false), 120)}
            onKeyDown={(event) => {
              if (event.key === "Escape") setOpen(false)
              if (event.key === "Backspace" && !look && chosen.length) {
                onPick(chosen[chosen.length - 1]!)
              }
            }}
            placeholder={chosen.length ? "" : "any"}
            className="min-w-16 flex-1 bg-transparent text-[12px] outline-none
                       placeholder:text-muted-foreground/70"
          />
          <span className="ml-auto flex shrink-0 items-center gap-1 self-center pl-1">
            {chosen.length > 0 && (
              <button
                type="button"
                title={`clear ${title}`}
                onClick={(event) => {
                  event.stopPropagation()
                  chosen.forEach(onPick)
                }}
                className="text-muted-foreground hover:text-foreground"
              >
                <X className="size-3.5" />
              </button>
            )}
            <ChevronDown className="size-3.5 text-muted-foreground/70" />
          </span>
        </div>

        {open && (
          <CommandList className="mt-1 max-h-44 rounded-md border">
            <CommandEmpty className="py-2 text-center text-[12px] text-muted-foreground">
              Nothing under that.
            </CommandEmpty>
            {groups.map((group) => (
              <CommandGroup key={group} heading={group || undefined}>
                {choices
                  .filter((one) => (one.under ?? "") === group)
                  .map((one) => (
                    <CommandItem
                      key={one.value}
                      value={`${one.name} ${one.under ?? ""}`}
                      onSelect={() => take(one.value)}
                      className={cn(
                        "gap-2 text-[13px] text-muted-foreground",
                        chosen.includes(one.value) &&
                          "font-medium text-teal-700 dark:text-teal-300",
                      )}
                    >
                      {one.dot && (
                        <span
                          className={cn("size-1.5 shrink-0 rounded-xs", TONE[one.dot].fill)}
                        />
                      )}
                      <span className={cn("truncate", mono && "font-mono")} title={one.name}>
                        {one.name}
                      </span>
                      <span className="ml-auto shrink-0 text-xs tabular-nums opacity-70">
                        {one.count}
                      </span>
                    </CommandItem>
                  ))}
              </CommandGroup>
            ))}
          </CommandList>
        )}
      </Command>
    </div>
  )
}

export function Filters({ runs }: { runs: Run[] }) {
  const { search, tags, set, search_, toggleTag } = useStore((state) => state)
  const terms = termsOf(search)
  const picked = tags.length + terms.length
  const databases = new Set(runs.flatMap((run) => run.databases)).size

  /** The terms of one field that are on, as the values they name. */
  const on = (field: string) =>
    terms
      .filter((term) => term.startsWith(`${field}:`))
      .map((term) => term.slice(field.length + 1))
  const pick = (field: string) => (value: string) =>
    search_(withTerm(search, `${field}:${value}`))

  const short = (name: string) => name.split("/").slice(-2).join("/")
  const worth = WORTH.map(([term, said]) => ({
    value: term,
    name: said,
    count: runs.filter(asQuery(term)).length,
  })).filter((one) => one.count > 0)
  const tables = [...tablesOn(runs)].flatMap(([database, held]) =>
    [...held]
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .map(([table, many]) => ({
        value: table,
        name: table,
        count: many,
        under: databases > 1 ? database : undefined,
      })),
  )

  return (
    <Popover>
      <PopoverTrigger
        className={cn(
          "flex h-8 items-center gap-1.5 rounded-md border px-2 text-[13px]",
          "text-muted-foreground hover:bg-accent",
          picked > 0 && "border-teal-600/40 text-teal-700 dark:text-teal-300",
        )}
      >
        <Filter className="size-3.5" />
        {picked > 0 ? picked : "filter"}
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="flex max-h-[70vh] w-[22rem] max-w-[calc(100vw-2rem)] flex-col gap-3
                   overflow-y-auto p-3"
      >
        <Field
          title="worth a look"
          choices={worth}
          chosen={worth.filter((one) => terms.includes(one.value)).map((one) => one.value)}
          onPick={(value) => search_(withTerm(search, value))}
        />
        <Field
          title="app"
          mono
          choices={tally(runs, (run) => [run.app]).map(([name, many]) => ({
            value: name,
            name: short(name),
            count: many,
          }))}
          chosen={on("app")}
          onPick={pick("app")}
        />
        <Field
          title="database"
          mono
          choices={tally(runs, (run) => run.databases).map(([name, many]) => ({
            value: name,
            name,
            count: many,
          }))}
          chosen={on("db")}
          onPick={pick("db")}
        />
        <Field
          title="tags"
          choices={tally(runs, (run) => run.tags).map(([tag, many]) => ({
            value: tag,
            name: tag,
            count: many,
          }))}
          chosen={tags}
          onPick={toggleTag}
        />
        <Field
          title="statements"
          choices={tally(runs, (run) => Object.keys(run.kinds)).map(([kind, many]) => ({
            value: kind,
            name: kind,
            count: many,
            dot: kind as Kind,
          }))}
          chosen={on("kind")}
          onPick={pick("kind")}
        />
        <Field
          title="tables"
          mono
          choices={tables}
          chosen={on("table")}
          onPick={pick("table")}
        />

        {picked > 0 && (
          <button
            type="button"
            onClick={() => {
              set("tags", [])
              search_("")
            }}
            className="flex items-center gap-1 self-start rounded-md px-1.5 py-1 text-[12px]
                       text-muted-foreground hover:bg-accent hover:text-foreground"
          >
            <X className="size-3" />
            clear {picked} filter{picked === 1 ? "" : "s"}
          </button>
        )}
      </PopoverContent>
    </Popover>
  )
}
