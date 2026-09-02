/** What a browser gives the page and the test runner does not. */

const kept = new Map<string, string>()

Object.defineProperty(globalThis, "localStorage", {
  value: {
    getItem: (name: string) => kept.get(name) ?? null,
    setItem: (name: string, value: string) => kept.set(name, value),
    removeItem: (name: string) => kept.delete(name),
  },
  configurable: true,
})
