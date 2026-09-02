/**
 * Where the recordings come from.
 *
 * A written report carries them on `window.SQLAKit`. A served page asks the
 * server for what it holds, and then listens for the rest.
 */

import type { Recording } from "@/lib/records"
import { useStore } from "@/lib/store"

declare global {
  interface Window {
    SQLAKit?: { records: Recording[]; about?: string }
  }
}

export function listen(): void {
  const { keep, set } = useStore.getState()
  const written = typeof window === "undefined" ? undefined : window.SQLAKit

  if (written) {
    written.records.forEach(keep)
    set("live", "kept")
    set("about", written.about || "report")
    return
  }

  void fetch("/records")
    .then((response) => response.json())
    .then((records: Recording[]) => records.forEach(keep))
    .catch(() => set("live", "no"))

  // After the page has loaded: an open stream never ends, and the browser
  // would count the page as still loading for as long as it is held.
  const stream = () => {
    const source = new EventSource("/stream")
    source.onmessage = (event) => keep(JSON.parse(event.data) as Recording)
    source.onerror = () => set("live", "no")
  }
  if (document.readyState === "complete") stream()
  else window.addEventListener("load", stream)
}

/** Forget everything, here and on the server. */
export function forget(): void {
  useStore.getState().clear()
  void fetch("/records", { method: "DELETE" }).catch(() => {})
}
