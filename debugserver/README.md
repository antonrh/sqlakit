# The debug server's page

The page `sqlakit debugserver` hands out, and the one `pytest --sqlakit-report`
writes. React, Tailwind and `shadcn/ui`, with `shiki` colouring the SQL.

```console
$ bun install
$ bun run dev     # the page on its own, at localhost:3000
$ bun test        # what the search, the SQL and the records add up to
$ bun run dist    # writes ../sqlakit/debugserver.html, one file
```

`bun run dist` is what the Python package ships: the script and the stylesheet
are written into the HTML, so the server has one file to hand out and a report
is that file with the recordings in it. Run it whenever this directory changes,
and commit the result.

The page reads `window.SQLAKit` when a report carries the recordings, and asks
`/records` and `/stream` when a server is serving it.
