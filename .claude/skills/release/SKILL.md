---
name: release
description: "Cut a release of SQLAKit. Use when asked to release, publish, bump the version, or write release notes. Covers the version bump, the tag that publishes, and what the notes have to say."
---

# Releasing SQLAKit

A release is a version bump, a tag, and the notes that tell people what
changed. Publishing itself is automatic: `.github/workflows/release.yml` runs
on a tag that names a version, checks the tag against `pyproject.toml`, runs
the linters and the tests again, and uploads to PyPI through trusted
publishing. No token lives in the repository.

## Before you start

The tag builds from whatever the working tree says, so leave it clean:

```console
$ uv run poe lint
$ uv run pytest
$ uv run mkdocs build --strict
```

`poe lint` covers `ruff`, `ty`, `codespell` and `tools/lint_docs.py`. The
integration tests need PostgreSQL and MySQL in containers, and `poe test` skips
them. Run `poe test-integration` before a release that touched SQL the two
servers compile differently.

## Choosing the number

The project is on `0.x`, so the minor number carries the weight:

- **Something breaks** for a user who upgrades: bump the minor. A renamed
  argument, a changed default, an exception that is now a different class, a
  query that emits different SQL. `0.3.1` -> `0.4.0`.
- **Nothing breaks**: bump the patch. Fixes, documentation, a new argument with
  a default. `0.3.1` -> `0.3.2`.

When in doubt, ask what a user's code does the moment they upgrade without
reading anything. If it can stop working, that is a minor.

## The release itself

```console
$ uv version 0.4.0           # writes pyproject.toml and uv.lock
$ git commit -a -m "Release 0.4.0"
$ git push
$ git tag 0.4.0 && git push origin 0.4.0
```

The tag has no `v`, matching every tag before it. The workflow refuses a tag
that does not match the version in `pyproject.toml`, so a mistyped one fails
before anything is published.

## The notes are the changelog

There is no `CHANGELOG.md`, on purpose. The GitHub release notes hold that
history, and duplicating them in the repository means keeping two of them in
step. Write the notes on the release itself.

Write them for someone deciding whether to upgrade, in the voice the
documentation uses. Group them by what a reader cares about, a bullet a change,
and link each to the pull request it came from, the way the generated list
does:

```markdown
## Breaking

* `order_by` takes `ignore_case` in place of `ci_fields`, and asks the dialect
  how to compare. by @antonrh in https://github.com/antonrh/sqlakit/pull/20
```

Say what a reader has to do differently. A breaking change gets the old code
and the new code side by side, not a sentence about "improved behaviour". A fix
says what was wrong, so a reader can tell whether it bit them. Keep the
generated list of pull requests under a `<details>` block at the bottom, above
the full changelog link.

Leave out what nobody outside the repository can see: refactors, test coverage,
lockfile updates.

## Once, on the publishing side

Trusted publishing needs a publisher on PyPI (owner `antonrh`, repository
`sqlakit`, workflow `release.yml`, environment `release`) and a `release`
environment on GitHub. Without them the workflow runs green until the upload
step and fails there with `invalid-publisher`. The environment is also where a
required reviewer goes, if a release should wait for a person.
