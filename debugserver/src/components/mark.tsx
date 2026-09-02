/** The library's mark: a database, and what a block ran inside it. */

import { cn } from "@/lib/utils"

export function Mark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden className={cn("size-5", className)}>
      <ellipse cx="12" cy="5" rx="7.5" ry="2.7" stroke="currentColor" strokeWidth="1.9" />
      <path
        d="M4.5 5v13.6c0 1.5 3.36 2.7 7.5 2.7 1.5 0 2.92-.16 4.1-.45"
        stroke="currentColor"
        strokeWidth="1.9"
        strokeLinecap="round"
      />
      <path d="M19.5 5v4.8" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" />
      <rect x="8" y="11.6" width="11.5" height="2.5" rx="1.25" fill="currentColor" />
      <rect x="8" y="15.8" width="7" height="2.5" rx="1.25" fill="currentColor" />
    </svg>
  )
}
