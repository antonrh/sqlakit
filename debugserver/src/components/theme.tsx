/** Light or dark, and back to whatever the system says. */

import { Moon, Sun } from "lucide-react"
import { useEffect, useState } from "react"

const KEY = "sqlakit.theme"

function nowDark(): boolean {
  const chosen = localStorage.getItem(KEY)
  if (chosen) return chosen === "dark"
  return window.matchMedia("(prefers-color-scheme: dark)").matches
}

export function useTheme() {
  const [dark, setDark] = useState(() => (typeof window === "undefined" ? false : nowDark()))

  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark)
  }, [dark])

  return {
    dark,
    flip: () => {
      localStorage.setItem(KEY, dark ? "light" : "dark")
      setDark(!dark)
    },
    system: () => {
      localStorage.removeItem(KEY)
      setDark(nowDark())
    },
  }
}

export function ThemeButton() {
  const { dark, flip, system } = useTheme()
  return (
    <button
      type="button"
      onClick={flip}
      onDoubleClick={system}
      title={`${dark ? "light" : "dark"} mode, or double-click to follow the system`}
      className="rounded-md p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground"
    >
      {dark ? <Sun className="size-4" /> : <Moon className="size-4" />}
    </button>
  )
}
