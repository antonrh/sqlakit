/** Any code, coloured. */

import { coloured } from "@/lib/highlight"
import { cn } from "@/lib/utils"

type Props = { code: string; lang?: "sql" | "python"; className?: string }

export function Code({ code, lang = "sql", className }: Props) {
  return (
    <pre
      className={cn("code", className)}
      dangerouslySetInnerHTML={{ __html: coloured(code, lang) }}
    />
  )
}
