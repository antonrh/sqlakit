/** Recordings a second, over the last minute. */

import { cn } from "@/lib/utils"
import type { Run } from "@/lib/records"

const SECONDS = 60

export function Strip({ runs, now }: { runs: Run[]; now: number }) {
  const second = Math.floor(now / 1000)
  const buckets = new Array<number>(SECONDS).fill(0)
  for (const run of runs) {
    const back = second - Math.floor(run.at / 1000)
    if (back >= 0 && back < SECONDS) buckets[SECONDS - 1 - back]! += 1
  }
  if (!buckets.some(Boolean)) return null
  const top = Math.max(...buckets, 1)

  return (
    <span
      className="flex h-5 items-end gap-px"
      title="recordings a second, over the last minute"
    >
      {buckets.map((many, index) => (
        <i
          key={index}
          style={{ height: `${Math.max(1, Math.round((many / top) * 18))}px` }}
          className={cn(
            "w-[3px] rounded-xs bg-teal-600 dark:bg-teal-300",
            index > SECONDS - 4 ? "opacity-90" : "opacity-35",
          )}
        />
      ))}
    </span>
  )
}
