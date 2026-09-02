/** Any code, coloured once the grammar has loaded. */

import { useEffect, useState } from "react"

import { coloured, highlighter } from "@/lib/highlight"

type Props = { code: string; lang?: "sql" | "python"; className?: string }

export function Code({ code, lang = "sql", className }: Props) {
  const [html, setHtml] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    void highlighter().then((shiki) => {
      if (live) setHtml(coloured(shiki, code, lang))
    })
    return () => {
      live = false
    }
  }, [code, lang])

  if (html === null) {
    return <pre className={className}>{code}</pre>
  }
  return <div className={className} dangerouslySetInnerHTML={{ __html: html }} />
}
