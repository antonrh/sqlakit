/** Every statement of a run, as wide as it was slow, in the order they ran. */

import { cn } from "@/lib/utils"
import { kindOf } from "@/lib/sql"
import { ms } from "@/lib/time"
import { TONE } from "@/lib/tone"
import type { Run, Statement } from "@/lib/records"

type Props = {
  run: Run
  narrow: ((one: Statement) => boolean) | null
  onPick: (index: number) => void
}

export function Waterfall({ run, narrow, onPick }: Props) {
  const whole = Math.max(run.milliseconds, 0.001)
  return (
    <div className="flex w-full gap-px px-4 pb-2">
      {run.statements.map((one, index) => {
        const kind = kindOf(one.sql)
        return (
          <button
            key={index}
            type="button"
            onClick={() => onPick(index)}
            style={{ flex: Math.max(one.milliseconds, 0.001) / whole }}
            title={`${index + 1}. ${kind.toUpperCase()} ${ms(one.milliseconds)}`}
            className={cn(
              "h-2 min-w-0.5 rounded-xs transition-opacity hover:opacity-70",
              TONE[kind].fill,
              narrow && !narrow(one) && "opacity-20",
            )}
          />
        )
      })}
    </div>
  )
}
