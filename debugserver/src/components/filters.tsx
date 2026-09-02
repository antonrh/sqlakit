/** What there is to narrow by, counted, behind one button. */

import { Filter, X } from "lucide-react"

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

function tally(runs: Run[], of: (run: Run) => string[]): [string, number][] {
  const counted = new Map<string, number>()
  for (const run of runs) {
    for (const one of of(run)) counted.set(one, (counted.get(one) ?? 0) + 1)
  }
  return [...counted].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
}

function Row({
  label,
  count,
  on,
  onPick,
  dot,
}: {
  label: React.ReactNode
  count: number
  on: boolean
  onPick: () => void
  dot?: Kind
}) {
  return (
    <button
      type="button"
      onClick={onPick}
      className={cn(
        "flex w-full items-baseline gap-2 rounded-md px-2 py-1 text-left text-[13px]",
        "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
        on && "bg-accent font-medium text-teal-700 dark:text-teal-300",
      )}
    >
      {dot && <span className={cn("size-1.5 shrink-0 rounded-xs", TONE[dot].fill)} />}
      <span className="truncate">{label}</span>
      <span className="ml-auto text-xs tabular-nums opacity-70">{count}</span>
    </button>
  )
}

function Group({ title, rows }: { title: string; rows: React.ReactNode[] }) {
  if (!rows.length) return null
  return (
    <div className="mb-2">
      <div
        className="px-2 pb-0.5 text-[11px] font-semibold uppercase tracking-wider
                      text-muted-foreground/70"
      >
        {title}
      </div>
      {rows}
    </div>
  )
}

export function Filters({ runs }: { runs: Run[] }) {
  const { search, app, tags, set, search_, toggleTag } = useStore((state) => state)
  const terms = termsOf(search)
  const picked = (app ? 1 : 0) + tags.length + terms.length
  const apps = tally(runs, (run) => [run.app])
  const short = (name: string) => name.split("/").slice(-2).join("/")
  // Which database each table was read or written on, over every recording.
  const touched = new Map<string, string[]>()
  for (const run of runs) {
    for (const [table, on] of Object.entries(run.tablesOn)) {
      const known = touched.get(table) ?? []
      touched.set(table, [...known, ...on.filter((one) => !known.includes(one))])
    }
  }
  const databases = new Set(runs.flatMap((run) => run.databases)).size

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
      <PopoverContent align="start" className="max-h-[70vh] w-64 overflow-y-auto p-2">
        {picked > 0 && (
          <button
            type="button"
            onClick={() => {
              set("app", "")
              set("tags", [])
              search_("")
            }}
            className="mb-2 flex w-full items-center gap-1 rounded-md px-2 py-1 text-[13px]
                       text-amber-600 hover:bg-accent dark:text-amber-400"
          >
            <X className="size-3" />
            clear {picked} filter{picked === 1 ? "" : "s"}
          </button>
        )}

        <Group
          title="worth a look"
          rows={WORTH.map(([term, label]) => {
            const many = runs.filter(asQuery(term)).length
            return many === 0 ? null : (
              <Row
                key={term}
                label={label}
                count={many}
                on={terms.includes(term)}
                onPick={() => search_(withTerm(search, term))}
              />
            )
          }).filter(Boolean)}
        />

        <Group
          title="app"
          rows={
            apps.length > 1
              ? [
                  <Row
                    key="all"
                    label="everything"
                    count={runs.length}
                    on={!app}
                    onPick={() => set("app", "")}
                  />,
                  ...apps.map(([name, many]) => (
                    <Row
                      key={name}
                      label={
                        <span className="font-mono" title={name}>
                          {short(name)}
                        </span>
                      }
                      count={many}
                      on={app === name}
                      onPick={() => set("app", name)}
                    />
                  )),
                ]
              : []
          }
        />

        <Group
          title="tags"
          rows={tally(runs, (run) => run.tags).map(([tag, many]) => (
            <Row
              key={tag}
              label={tag}
              count={many}
              on={tags.includes(tag)}
              onPick={() => toggleTag(tag)}
            />
          ))}
        />

        <Group
          title="statements"
          rows={tally(runs, (run) => Object.keys(run.kinds)).map(([kind, many]) => (
            <Row
              key={kind}
              label={kind}
              dot={kind as Kind}
              count={many}
              on={terms.includes(`kind:${kind}`)}
              onPick={() => search_(withTerm(search, `kind:${kind}`))}
            />
          ))}
        />

        <Group
          title="tables"
          rows={tally(runs, (run) => run.tables).map(([table, many]) => (
            <Row
              key={table}
              label={
                <span className="font-mono">
                  {table}
                  {databases > 1 && (
                    <span className="ml-1.5 text-muted-foreground">
                      {(touched.get(table) ?? []).join(" · ")}
                    </span>
                  )}
                </span>
              }
              count={many}
              on={terms.includes(`table:${table}`)}
              onPick={() => search_(withTerm(search, `table:${table}`))}
            />
          ))}
        />
      </PopoverContent>
    </Popover>
  )
}
