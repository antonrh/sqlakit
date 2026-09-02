/** Where a statement came from: the nearest frame, and the stack behind it. */

import { ChevronRight } from "lucide-react"
import { useState } from "react"

import { called, frame } from "@/lib/time"
import { cn } from "@/lib/utils"

export function Trace({ stack, label }: { stack: string[]; label: string | null }) {
  const [shown, setShown] = useState(false)
  if (!stack?.length) return null
  const rest = stack.slice(1)
  const here = called(stack[0]!, label)

  // The full path in the tooltip: `line 225` alone leaves you guessing which
  // file it is a line of.
  if (!rest.length) {
    return (
      <div className="mt-1.5 font-mono text-[11px] text-muted-foreground" title={stack[0]}>
        {here}
      </div>
    )
  }

  return (
    <div className="mt-1.5 text-[11px]">
      <button
        type="button"
        onClick={() => setShown(!shown)}
        title={stack[0]}
        className="inline-flex items-center gap-0.5 font-mono text-muted-foreground
                   hover:text-foreground"
      >
        {here}
        <ChevronRight className={cn("size-3 transition-transform", shown && "rotate-90")} />
      </button>
      {shown && (
        <div className="mt-1 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-0.5 font-mono">
          {stack.map((line, index) => {
            const { name, where } = frame(line)
            return (
              <div key={index} className="contents">
                <span
                  className={cn(index === 0 ? "text-foreground/80" : "text-muted-foreground")}
                >
                  {name}
                </span>
                <span className="truncate text-muted-foreground/70" title={line}>
                  {where}
                </span>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
