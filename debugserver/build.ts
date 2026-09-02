#!/usr/bin/env bun
/**
 * Build the page into one file.
 *
 * The Python package ships a single `debugserver.html`: the server hands it
 * out as it is, and a written report is that same file with the recordings
 * added to it. So the script and the stylesheet are written into the HTML
 * rather than left beside it.
 */

import { mkdir, rm } from "node:fs/promises"
import { dirname, join } from "node:path"

import plugin from "bun-plugin-tailwind"

const WHERE = process.argv[2] ?? "../sqlakit/debugserver.html"
const STAGING = ".build"

await rm(STAGING, { recursive: true, force: true })

const built = await Bun.build({
  entrypoints: ["src/index.html"],
  outdir: STAGING,
  target: "browser",
  minify: true,
  sourcemap: "none",
  plugins: [plugin],
  define: { "process.env.NODE_ENV": JSON.stringify("production") },
})

if (!built.success) {
  console.error(built.logs.join("\n"))
  process.exit(1)
}

const read = (name: string) => Bun.file(join(STAGING, name)).text()

let page = await read("index.html")

// The replacement is a function: a bundle is full of `$&` and `$'`, which
// `replace` would read as instructions rather than as text.
for (const found of [...page.matchAll(/<script[^>]*src="\.\/([^"]+\.js)"[^>]*><\/script>/g)]) {
  // A `</script>` inside the bundle would end the tag it now travels in.
  const code = (await read(found[1]!)).replaceAll("</script", "<\\/script")
  page = page.replace(found[0], () => `<script type="module">${code}</script>`)
}

for (const found of [...page.matchAll(/<link[^>]*href="\.\/([^"]+\.css)"[^>]*\/?>/g)]) {
  const style = (await read(found[1]!)).replaceAll("</style", "<\\/style")
  page = page.replace(found[0], () => `<style>${style}</style>`)
}

await mkdir(dirname(WHERE), { recursive: true })
await Bun.write(WHERE, page)
await rm(STAGING, { recursive: true, force: true })

const size = (page.length / 1024).toFixed(0)
console.log(`${WHERE}  ${size} KB, one file`)
