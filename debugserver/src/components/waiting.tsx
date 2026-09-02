/** The page before the first recording: where to send one, and how. */

import { Check, Copy } from "lucide-react"
import { useState } from "react"

import { Code } from "@/components/code"
import { cn } from "@/lib/utils"

/** The server this page came from, which is where recordings go. */
function where(): string {
  const here = typeof location === "undefined" ? "" : location.host
  return here && !here.startsWith("file") ? here : "localhost:5555"
}

export function Waiting() {
  const [copied, setCopied] = useState(false)
  const [host, port] = where().split(":")
  const block =
    `with db.recording("GET /users", debugserver=("${host}", ${port ?? 5555})):\n` +
    `    list_users()`

  const copy = () => {
    void navigator.clipboard.writeText(block).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1200)
    })
  }

  return (
    <div className="flex h-full flex-col items-center justify-center gap-6 p-10">
      <div className="flex flex-col items-center gap-3">
        <span className="relative flex size-3">
          <span
            className="absolute inline-flex size-full animate-ping rounded-full
                       bg-teal-500 opacity-60"
          />
          <span className="relative inline-flex size-3 rounded-full bg-teal-600 dark:bg-teal-300" />
        </span>
        <h2 className="text-sm font-semibold">Listening on {where()}</h2>
        <p className="text-sm text-muted-foreground">
          Record a block, and what it ran appears here.
        </p>
      </div>

      <div className="group relative w-full max-w-2xl">
        <Code lang="python" code={block} className="sql rounded-xl border bg-card px-5 py-4" />
        <button
          type="button"
          onClick={copy}
          title="copy"
          className={cn(
            "absolute right-3 top-3 rounded-md p-1 text-muted-foreground",
            "opacity-0 hover:bg-accent hover:text-foreground group-hover:opacity-100",
          )}
        >
          {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
        </button>
      </div>

      <p className="text-xs text-muted-foreground">
        <code className="font-mono">pytest --sqlakit-report</code> writes the same page for a
        test run, as a file that opens without a server.
      </p>
    </div>
  )
}
