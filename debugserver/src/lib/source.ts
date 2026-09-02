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
  if (document.readyState === "complete") stream()
  else window.addEventListener("load", () => stream())
}

/** How long to wait before reopening a stream the browser gave up on. */
const RETRY = 2000

/**
 * Listen for what arrives, until the server stops, and then until it is back.
 *
 * A browser reopens a stream it lost on its own. One it has given up on, a
 * refused connection among them, is ours to reopen, so a server started again
 * is found either way. ``retry`` is how long that waits.
 */
export function stream(retry: number = RETRY): void {
  const { keep, set } = useStore.getState()
  const source = new EventSource("/stream")
  source.onopen = () => set("live", "yes")
  source.onmessage = (event) => keep(JSON.parse(event.data) as Recording)
  source.onerror = () => {
    set("live", "no")
    if (source.readyState === EventSource.CLOSED) setTimeout(() => stream(retry), retry)
  }
}

/** Forget everything, here and on the server. */
export function forget(): void {
  useStore.getState().clear()
  void fetch("/records", { method: "DELETE" }).catch(() => {})
}
