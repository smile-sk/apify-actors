# PyPI Package API - Versions, Dependencies & License

Send PyPI project names and get one row per project: the version you asked for (or the
latest one), licence, `requires-python`, release count, first and latest release date,
dependencies, supported Python versions and the size of the release files. No API key, no
browser, no proxy.

A name that PyPI does not have is a **verdict**: the row comes back free with the reason, so
a dependency audit does not pay for packages that do not exist.

## Input

| field | what it is |
|---|---|
| `packages` | PyPI project names. Each line is a bare name (`requests`), a pinned version (`requests==2.31.0`) or a project link (`https://pypi.org/project/requests/`, optionally with a version). Names are compared the way PyPI compares them, so `Requests`, `zope.interface` and `zope-interface` are one project and are charged once. |
| `maxPackages` | How many packages to read and sell. Default 100, maximum 500. |
| `includeDependencies` | Put the release dependencies (`requires_dist`, first 50) into the row. On by default. |

## Output, one row per package

`name`, `version`, `summary`, `license`, `requiresPython`, `author`, `homepage`, `repository`,
`pythonVersions`, `dependencies`, `dependenciesCount`, `dependenciesTruncated`,
`releaseCount`, `firstRelease`, `latestRelease`, `filesInRelease`, `releaseSizeBytes`,
`classifiers`, `source`, `billable`, plus `actorIndex` and `actorInput` so the input order can
be rebuilt.

A pinned version returns that version, not the newest one: `numpy==2.0.0` reports 2.0.0 even
though PyPI has later releases, and the file sizes are those of that release.

Free status rows carry `status` and `reason` instead of package fields:

- `INVALID_INPUT` - the line is not a package name (a link on another host, an empty line, a
  name longer than PyPI allows).
- `DUPLICATE` - the same project was already sold in this run.
- `NOT_FOUND` - a valid name, but PyPI has no project behind it.
- `DUPLICATE` - the same project was already sold in this run. Two versions of one project
  are one project: the first line is sold, the second is free.
- `NOT_FOUND` - a valid name, but PyPI has no project behind it, or no release with the
  version you pinned. Nothing is charged for a version PyPI never published, and the reason
  names the latest version that does exist.
- `NOT_CHECKED_TIME`, `NOT_CHECKED_CAP`, `NOT_CHECKED_LIMIT`, `NOT_CHECKED_BILLING`,
  `NOT_CHECKED_ERROR` - the line was not read, and the reason says why: the deadline, your
  spending limit (`CAP`), the `maxPackages` slice (`LIMIT`), an unreadable price, or PyPI
  refusing the read.

## Price

$0.80 per 1,000 packages: `package-delivered` is charged once per project that came back with
metadata. A missing name, a malformed line and a duplicate cost nothing. The lowest spending
limit you can set per run is $0.05.

## Politeness and limits

PyPI's JSON API is keyless and asks callers to identify themselves, so every request carries a
`User-Agent` that names this actor and its page. Names go out one at a time, and there is a
short pause between chunks rather than between every name. The run reads at most 500 packages
per run; the rest comes back as free status rows with the
reason, never as a silent cut.

## What this actor does not do

- It does not search PyPI by keyword, author or classifier: it reads the names you give it.
- It does not download the package files or the source: `releaseSizeBytes` and
  `filesInRelease` describe the release, the files are yours to fetch.
- It does not resolve the dependency tree: `dependencies` is the declaration from the
  metadata, not a resolution, and it is capped at 50 entries with `dependenciesTruncated` set.
- It does not read the README or the long description of a project.

## Source

Data comes from the official PyPI JSON API (`pypi.org/pypi/<name>/json`), keyless. Metadata is
reported as PyPI returns it; empty fields stay `null` rather than being invented. PyPI is
operated by the Python Software Foundation and subject to its own terms of use.
