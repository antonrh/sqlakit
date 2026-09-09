---
name: docs
description: "Write or review SQLAKit's documentation. Use when editing anything under docs/, README.md or a docstring that the reference renders, and before committing such a change. Covers the voice the docs are written in, what belongs on which page, and the checks that keep the examples true."
---

# Writing SQLAKit's Documentation

The documentation serves both prospective users evaluating the library and active users building with it. Both require clear statements of behavior accompanied by runnable code.

---

## The Voice

Write as you would explain a concept to a peer. The Django tutorial is the benchmark: address the reader directly ("you"), use contractions, and keep one idea per sentence.

### Style Guide Rules

* **Aphorisms and Closers:** Omit clever wrap-ups. End a section as soon as the technical point is complete.
* **Marketing Language:** Avoid terms like *seamless*, *powerful*, *blazing-fast*, or *effortless*. Provide benchmark numbers or explicit performance characteristics instead.
* **Filler Words:** Omit *simply*, *just*, *easily*, *obviously*, and *of course*.
* **First Person:** Avoid *we*, *our*, or *let's*. Refer directly to the library or the user.
* **Personification:** Code and queries do not "want" or "know". They *raise*, *return*, *yield*, or *hold*.
* **Active Voice:** Write "`SQLAKit` parses the dialect", not "the dialect is parsed".
* **Phrasal Verbs:** Use standard verbs (*starts*, *ends*) instead of phrasal variants (*spins up*, *ends up*).
* **Punctuation:** Avoid em-dashes, semicolons, and exclamation marks in prose. Use periods.

Identifiers, library names, types, and user-typed keywords must be enclosed in backticks (e.g., `SQLAKit`, `Database`, `asyncio`). Wrap prose at 80 columns.

---

## Headings Standard

A heading must reflect the user's search intent using appropriate phrasing:

| Heading Type | Purpose | Example |
| :--- | :--- | :--- |
| **Imperative Verb** | Actionable procedures and step-by-step tasks | `Install`, `Write a test`, `Configure connections` |
| **Noun Phrase** | Architectural concepts, parameters, or reference sections | `Database URL`, `Engine configuration`, `Defaults` |
| **Direct Question** | Targeted troubleshooting or complex conceptual queries | `Where does a model live?`, `Which database is active?` |

### Consistency Principles
* **Single Naming Standard:** Use identical terminology across all pages (e.g., use `Soft deletes` consistently rather than mixing with `Logical deletion`).
* **Section Formatting:** Use sentence case with no trailing colon.
* **Anchor Integrity:** When renaming headings, update all relative markdown links and explicit `{#anchor}` targets.

---

## Writing a Section

Every section, not only the ones that introduce a setting, is written for a
reader who does not yet know the vocabulary. Three questions, in this order:

1. **Is this mine?** The situation, in the reader's words and concretely: "a
   handler, a consumer, a scheduled job", not "an unbound execution context".
   Name a case where the answer is no, so the reader can leave early.
2. **What do I do?** The shortest path: a command to run, a line to add. How to
   read what it prints beats a paragraph on why it prints that.
3. **What changes for me?** One example after the change, and the way back out.

The mechanism comes last, in one or two sentences, and only where it changes
what the reader would type. A section that opens on how something works inside
has the order backwards.

The test: read the section to someone who has the problem and not the words for
it. If the first thing they learn is machinery rather than whether the section
is about them, rewrite it.

## Page Anatomy & Structure

Topic pages (`queries.md`, `context.md`, etc.) must follow a standardized layout:

1. **Quick Example:** A minimal, copy-pasteable snippet demonstrating the core feature (max 5-10 lines).
2. **Core Mechanics:** Concise explanation of the underlying behavior and default settings.
3. **Patterns & Options:** Itemized configurations, methods, or parameters (use Markdown tables for comparing options).
4. **Limits & Edge Cases:** Important constraints, async/sync differences, or exceptions raised.

---

## Executable Snippets

Every code block must be fully runnable. Provide necessary imports or structure the snippet as a clear continuation of the preceding block on the same page.

Nothing runs the blocks of a topic page, so a snippet you write or change is yours to run:

```console
$ cd /tmp && uv run --project ~/Projects/sqlakit python your_snippet.py
```

`tests/docs` covers two pages that are run in full: `getting-started.md`, assembled into the files and the script a reader types, and the `conftest.py` that `testing.md` prints.

### Code Block Requirements
* Inline code comments must not exceed one line. Place longer explanations in the preceding or following prose.
* Ensure type hints and code styles in snippets mirror the main codebase.

---

## Document Placement Guidelines

* **`README.md` and `docs/index.md`:** Primary entry points. Contain the high-level overview, installation steps, one complete working example, key differentiators, and navigation links. Keep both files synchronized.
* **`docs/getting-started.md`:** End-to-end tutorial building a single continuous application.
* **Topic Pages (`docs/*.md`):** Focused guides answering "how-to" questions for specific features.
* **`docs/reference.md`:** Autogenerated from docstrings via `mkdocstrings`. Place detailed technical explanations inside class/function docstrings.
* **`docs/examples.md`:** Complete, standalone runnable application scripts (e.g., FastAPI or Flask integrations).

---

## Verification & Automated Linting

Before committing documentation updates, run the validation suite:

```console
$ uv run poe lint                    # Runs ruff, ty, codespell, and lint_docs.py
$ uv run pytest tests/docs           # Runs the tutorial and the conftest the docs print
$ uv run mkdocs build --strict       # Verifies navigation, links, and cross-references