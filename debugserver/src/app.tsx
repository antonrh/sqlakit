/** The page: what is happening across the top, the recordings under it. */

import { Trash2 } from "lucide-react"
import { useEffect, useState } from "react"

import { Code } from "@/components/code"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import { Detail } from "@/components/detail"
import { List } from "@/components/list"
import { Strip } from "@/components/strip"
import { ThemeButton } from "@/components/theme"
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from "@/components/ui/resizable"
import { useDefaultLayout } from "react-resizable-panels"
import type { Run } from "@/lib/records"
import { asNarrowing, asQuery } from "@/lib/search"
import { forget, listen } from "@/lib/source"
import { useStore } from "@/lib/store"
import { cn } from "@/lib/utils"

const BY: Record<string, (a: Run, b: Run) => number> = {
  recent: (a, b) => b.at - a.at,
  slow: (a, b) => b.milliseconds - a.milliseconds,
  many: (a, b) => b.count - a.count,
  repeated: (a, b) => b.duplicates - a.duplicates,
  label: (a, b) => a.key.localeCompare(b.key) || b.at - a.at,
}

function Waiting() {
  return (
    <div className="p-10 text-center text-muted-foreground">
      <p>Waiting for the first recording.</p>
      <Code
        lang="python"
        className="sql mt-4 inline-block rounded-xl border bg-card px-5 py-4 text-left"
        code={`with db.recording("GET /users", debugserver=("localhost", 5555)):
    list_users()`}
      />
    </div>
  )
}

/** Forgetting reaches the server too, so it asks first. */
function Bin() {
  return (
    <AlertDialog>
      <AlertDialogTrigger
        title="forget everything, here and on the server"
        className="rounded-md p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground"
      >
        <Trash2 className="size-4" />
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Forget every recording?</AlertDialogTitle>
          <AlertDialogDescription>
            This clears the page and the server's history. What the applications send after it
            still arrives.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Keep them</AlertDialogCancel>
          <AlertDialogAction onClick={forget}>Forget</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

export function App() {
  const state = useStore((one) => one)
  const { runs, search, sort, app, tags, live, about, paused, missed } = state
  const [now, setNow] = useState(() => Date.now())
  const [picked, setPicked] = useState<number | null>(null)
  // The reader's own split, kept between visits.
  const panes = useDefaultLayout({
    id: "sqlakit.panes",
    panelIds: ["list", "detail"],
  })

  useEffect(() => {
    listen()
    const beat = setInterval(() => setNow(Date.now()), 15_000)
    const keys = (event: KeyboardEvent) => {
      const box = document.querySelector<HTMLInputElement>("[data-search]")
      if (event.key === "/" && document.activeElement !== box) {
        event.preventDefault()
        box?.focus()
      } else if (event.key === "Escape" && document.activeElement === box) {
        useStore.getState().search_("")
        box?.blur()
      }
    }
    document.addEventListener("keydown", keys)
    return () => {
      clearInterval(beat)
      document.removeEventListener("keydown", keys)
    }
  }, [])

  const wanted = asQuery(search)
  const narrow = asNarrowing(search)
  const shown = [...runs]
    .filter(
      (run) =>
        (!app || run.app === app) &&
        (!tags.length || run.tags.some((tag) => tags.includes(tag))) &&
        wanted(run),
    )
    .sort(BY[sort]!)
  const queries = shown.reduce(
    (sum, run) => sum + (narrow ? run.statements.filter(narrow).length : run.count),
    0,
  )
  // The newest, until the reader picks one to stay on.
  const open = shown.find((run) => run.id === picked) ?? shown[0]

  return (
    <div className="flex h-screen flex-col bg-background text-foreground">
      <header className="flex flex-wrap items-center gap-3 border-b px-4 py-2">
        <h1 className="font-mono text-base font-bold">
          <span className="text-teal-700 dark:text-teal-300">SQLA</span>Kit
        </h1>

        <button
          type="button"
          onClick={live === "kept" ? undefined : state.pause}
          title={
            live === "kept" ? "a written report, not a running server" : "pause the stream"
          }
          className={cn(
            "flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px]",
            "font-semibold uppercase tracking-wider",
            live === "no"
              ? "border-rose-500/40 bg-rose-500/10 text-rose-600 dark:text-rose-400"
              : live === "kept"
                ? "font-medium normal-case tracking-normal text-muted-foreground"
                : paused
                  ? "border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400"
                  : "border-teal-500/40 bg-teal-500/10 text-teal-700 dark:text-teal-300",
          )}
        >
          <span className="size-1.5 rounded-full bg-current" />
          {live === "kept"
            ? about
            : live === "no"
              ? "disconnected"
              : paused
                ? missed
                  ? `paused · ${missed} new`
                  : "paused"
                : "live"}
        </button>

        {live !== "kept" && <Strip runs={runs} now={now} />}

        <span className="flex items-baseline gap-4">
          <span className="flex items-baseline gap-1.5">
            <b className="text-base font-semibold tabular-nums">{shown.length}</b>
            <em className="text-[11px] uppercase not-italic tracking-wider text-muted-foreground">
              recordings
            </em>
          </span>
          <span className="flex items-baseline gap-1.5">
            <b className="text-base font-semibold tabular-nums">{queries}</b>
            <em className="text-[11px] uppercase not-italic tracking-wider text-muted-foreground">
              queries
            </em>
          </span>
        </span>

        <div className="ml-auto flex items-center gap-1">
          <ThemeButton />
          {live !== "kept" && <Bin />}
        </div>
      </header>

      <ResizablePanelGroup
        orientation="horizontal"
        className="min-h-0 flex-1"
        defaultLayout={panes.defaultLayout}
        onLayoutChanged={panes.onLayoutChanged}
      >
        <ResizablePanel
          id="list"
          defaultSize="40"
          minSize={320}
          maxSize="65"
          className="flex min-h-0 flex-col"
        >
          <List runs={runs} shown={shown} picked={open?.id ?? null} onPick={setPicked} />
        </ResizablePanel>
        <ResizableHandle withHandle />
        <ResizablePanel id="detail" className="min-h-0 overflow-y-auto">
          {open ? (
            <Detail run={open} narrow={narrow} />
          ) : (
            <p className="p-10 text-center text-muted-foreground">
              Nothing matches that search.
            </p>
          )}
        </ResizablePanel>
      </ResizablePanelGroup>
    </div>
  )
}
