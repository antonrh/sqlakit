---
name: docs
description: "Write or review SQLAKit's documentation. Use when editing anything under docs/, README.md or a docstring that the reference renders, and before committing such a change. Covers the voice the docs are written in, what belongs on which page, and the checks that keep the examples true."
---

# Writing SQLAKit's documentation

The documentation is read by people deciding whether to use the library and by
people who already have. Both want the same thing: a plain sentence that says
what happens, and an example that runs.

## The voice

Write the way you would explain it to a colleague. The Django tutorial is the
benchmark: address the reader as "you", use contractions, keep one idea per
sentence.

Do not write:

- **Aphorisms and clever closers.** A paragraph ends when the point is made.
- **Marketing adjectives**: seamless, powerful, blazing-fast, effortless. If
  the library is fast, give the measurement instead.
- **Filler**: simply, just, easily, obviously, of course. "Easily" tells the
  reader their trouble is small, which they get to decide.
- **The first person**: we, our, let's. The docs speak about the library.
- **Personification**: the query does not "want", "know" or "say". It raises,
  returns, holds.
- **Passive voice where the library acts**: "`SQLAKit` reads the dialect", not
  "the dialect is read".
- **Cleft constructions**: say "the session owns the transaction", not "the
  transaction is what the session owns".
- **Phrasal verbs**: it ends, not ends up. It starts, not spins up.
- **Em-dashes and semicolons in prose.** Use a full stop.
- **Exclamation marks.**

Identifiers, library names and types go in backticks, including `SQLAKit`
itself and every keyword a reader would type. Headings are sentence case, with
no trailing colon. Prose wraps at 80 columns.

`tools/lint_docs.py` catches the mechanical half of this. The rest is
judgement, and belongs to review.

## What goes where

- **`README.md` and `docs/index.md` are the shop window.** What the library is,
  how to install it, one example that runs, what makes it different, links
  onward. Not a tutorial. The two carry the same text and differ only in their
  links, so change both together.
- **`docs/getting-started.md`** builds one working thing end to end, each
  snippet continuing the last.
- **The topic pages** answer "how do I", one subject each: `databases.md`,
  `context.md`, `queries.md`, `sql.md`, `models.md`, `routing.md`,
  `debugging.md`, `testing.md`.
- **`docs/reference.md`** is generated from docstrings by `mkdocstrings`. Put
  the explanation in the docstring, not around the `:::` directive. A new
  public name belongs there the day it is added.
- **`docs/examples.md`** holds whole programs, not fragments.

Say a thing once. If two pages open with the same paragraph, one of them is an
overview and should say what the section holds instead.

## Every snippet runs

A block that cannot be pasted into a file and run is a bug. Give it its
imports, or make it an obvious continuation of the block above it on the same
page. Before you commit, run the blocks you touched:

```console
$ cd /tmp && uv run --project ~/Projects/sqlakit python your_snippet.py
```

Comments inside a block are one line. A block needing a paragraph of comment
needs the paragraph outside it instead.

## The docs must not outlive the code

A sentence about behaviour is a claim, and claims rot. When you write or
review one, check it against the source, or better, run it:

- Does the exception named actually get raised, and is it that class?
- Does the query really emit that SQL, on that dialect?
- Is the argument still called that, and does it still default to that?

If you cannot confirm a claim, do not soften it into vagueness. Test it, then
write what happened.

## Before committing

```console
$ uv run poe lint                    # ruff, ty, codespell, and lint_docs.py
$ uv run mkdocs build --strict       # links, nav, references resolve
```

Both run in CI, `poe lint` in the `lint` job and `mkdocs` in the `docs` job.
`lint_docs.py` checks that every fenced Python block formats and annotates
like the code around it, and that the prose keeps the habits above. It
deliberately does not check that a block is complete: many continue the one
above them.

It reads `docs/*.md` and nothing else, so `README.md` is on you.

Also worth a look when the change is larger than a sentence:

- relative links still resolve, including the anchors you renamed
- the nav in `mkdocs.yml` matches the headings you changed
- the page still reads top to bottom for someone who has not read the others
