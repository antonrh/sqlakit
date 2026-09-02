/** The colour a kind of statement is drawn in, in the bar and in the counts. */

import type { Kind } from "@/lib/sql"

export const TONE: Record<Kind, { text: string; fill: string }> = {
  select: { text: "text-teal-600 dark:text-teal-300", fill: "bg-teal-600 dark:bg-teal-300" },
  insert: {
    text: "text-emerald-600 dark:text-emerald-300",
    fill: "bg-emerald-600 dark:bg-emerald-300",
  },
  update: {
    text: "text-amber-600 dark:text-amber-300",
    fill: "bg-amber-600 dark:bg-amber-300",
  },
  delete: { text: "text-rose-600 dark:text-rose-300", fill: "bg-rose-600 dark:bg-rose-300" },
  other: { text: "text-muted-foreground", fill: "bg-muted-foreground" },
}
