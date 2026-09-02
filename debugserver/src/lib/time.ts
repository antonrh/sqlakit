/** Times, as the page says them. */

/**
 * Milliseconds, to the precision that tells them apart.
 *
 * Sub-millisecond statements are the common case on a warm database, and
 * rounding them all to `0.0 ms` hides which one cost anything.
 */
export function ms(value: number): string {
  const said = value < 1 ? value.toFixed(2) : value < 10 ? value.toFixed(1) : Math.round(value)
  return `${said} ms`
}

export function clock(at: number): string {
  const when = new Date(at)
  const pad = (number: number) => String(number).padStart(2, "0")
  return `${pad(when.getHours())}:${pad(when.getMinutes())}:${pad(when.getSeconds())}`
}

export function exactly(at: number): string {
  const when = new Date(at)
  return `${when.toLocaleString()}.${String(when.getMilliseconds()).padStart(3, "0")}`
}

export function ago(at: number, now: number = Date.now()): string {
  const seconds = Math.max(0, (now - at) / 1000)
  if (seconds < 5) return "just now"
  if (seconds < 90) return `${Math.round(seconds)}s ago`
  if (seconds < 5400) return `${Math.round(seconds / 60)}m ago`
  return `${Math.round(seconds / 3600)}h ago`
}

/**
 * Where a statement came from: `create_model:67`.
 *
 * The line alone when the function is the label above it. A test carries the
 * class it is in, `TestUsers::test_one`, and the frame knows only the method.
 */
export function called(frame: string, label?: string | null): string {
  const parts = frame.match(/^(.*):(\d+) in (.*)$/)
  if (!parts) return frame
  const named = label?.split("::").pop()
  return parts[3] === named ? `line ${parts[2]}` : `${parts[3]}:${parts[2]}`
}

/** A frame, split into the function and the end of the path it is in. */
export function frame(line: string): { name: string; where: string } {
  const parts = line.match(/^(.*):(\d+) in (.*)$/)
  if (!parts) return { name: line, where: "" }
  return {
    name: parts[3]!,
    where: `${parts[1]!.split("/").slice(-2).join("/")}:${parts[2]}`,
  }
}
