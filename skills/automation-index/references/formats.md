# Impact, diff and export formats

These commands read the index and nothing else, and need no login. They add no facts and change no findings. Like every command except `query`, they open `index.db` for writing, to bring its schema and derived tables (library closure, process links) up to date. Give them a writable copy.

## `impact`

```bash
node SKILL_DIR/scripts/automation-index.mjs --index-dir INDEX_DIR impact --kind KIND --name NAME [--format md|json]
node SKILL_DIR/scripts/automation-index.mjs --index-dir INDEX_DIR impact --top 20
```

| Kind | Matched against |
|---|---|
| `asset`, `credential` | `reads_*` and `writes_*` edges |
| `queue` | `uses_queue` edges; the `Mode` column shows `add` (writes) or `get` (reads) |
| `bucket`, `process`, `connection`, `database`, `connector-operation` | the edge of the same name (`invokes_process` for `process`) |
| `application` | `uses_application`; `outlook` and `C:\...\OUTLOOK.EXE` both match `outlook.exe` |
| `host` | `targets_url` and `calls_api`; pass a URL or a bare host |
| `library` | the dependency graph, through every indexed library in between |

A name with `*` or `?` is a glob. Matching ignores case. For `host`, a full URL is reduced to its host first, so the `?` of a query string is not read as a glob.

The output has four parts:

1. **Deployed processes** that reference the resource, directly or through a library (`Via library`). Each row has its folder, the number of references, the first `file:line`, total and 30-day runs, and enabled triggers. Rows are sorted by recent runs.
2. **Other indexed packages**: libraries, source folders and packages that are not deployed.
3. **One hop away**: processes linked to an affected one through a queue handover or a process call (`v_process_link`). They do not reference the resource, but they may notice when an affected process stops.
4. **What the index cannot rule out**: references of the same kind built at runtime, packages not fully read, and matches that rest only on low-confidence DLL strings.

Risk grade, computed from those counts:

| Grade | When |
|---|---|
| `high` | 5 or more deployed processes, or 3 or more that ran in the last 30 days, or 3 or more folders |
| `medium` | 2 or more deployed processes, or 1 that ran in the last 30 days |
| `low` | one deployed process, or only packages that are not deployed |
| `none` | no reference found |

The grade is marked as a lower bound whenever part 4 is not empty. Runs count only when the latest crawl read the job list.

`impact --top N` ranks the resources shared by the most deployed processes: assets, queues, hosts, applications, connections and libraries, counting only high- and medium-confidence edges. `INDEX.md` carries the top 15.

## `diff`

```bash
node SKILL_DIR/scripts/automation-index.mjs --index-dir INDEX_DIR diff --package ID --from V1 --to V2
node SKILL_DIR/scripts/automation-index.mjs --index-dir INDEX_DIR diff --baseline fingerprints-DATE.jsonl [--package GLOB]
```

`diff` compares fingerprints (the `export` format), field by field:

- **Sets:** applications, hosts, assets, credentials, queue writes and reads, buckets, processes called, connections, databases, email domains, connector operations, entry points, libraries and activity packages (with version), upstream and downstream automations, project documents, findings, coverage.
- **Values:** version, sources present, target framework, Studio version, expression language, workflow count, complexity counts.

With `--baseline`, packages are matched by id, ignoring case. When a side holds several versions of one package, the deployed one stands for it, then the highest in NuGet order (a release outranks its prereleases). The **New external reach** section lists applications, hosts, credentials, connections, databases, email domains and connector operations that appear on the new side.

Fingerprints leave out runtime-built values and low-confidence DLL strings. A value that changed from a literal to an expression shows as removed, with nothing added. Check every change in the source tree before reporting it.

## `export --format sarif`

SARIF 2.1.0 with one run. `review` fills the `finding` table, so run it first; `export` warns when the table is empty.

- **Rules:** every rule in [findings.md](findings.md), with `shortDescription`, `fullDescription`, `help` and a default level: Critical is `error`, Warning is `warning`, Info is `note`. AI-SEC-001 and AI-GOV-001 carry the `security` tag and a `security-severity` (9.0 and 7.0).
- **Locations:** when the evidence names a file that exists inside the package (links are resolved, and a link that leaves the package is not followed), the result points at it with an absolute `file://` URI and its line. For a project indexed with `--source` that is the project folder itself; otherwise it is the package's copy under `INDEX_DIR/src/`. A finding inherited from a library points at the library's file: its evidence ends with `(in LIBRARY@VERSION)`. A package-level finding with no file points at the package's `project.json`. Findings that belong to no package (AI-LIB-001, AI-RES-004, queue handovers) have no location. GitHub code scanning does not display those; SARIF viewers do.
- **GitHub:** run the export from the repository checkout that holds the sources. GitHub turns absolute URIs under the checkout into repository paths. Files under `INDEX_DIR/src/` are not in the repository, so their results show without a code annotation.
- **Fingerprints:** `partialFingerprints["automationIndexFinding/v1"]` is a hash of rule, package id, subject, file and activity, plus an occurrence number for findings that are otherwise identical. The activity is named by its Studio `IdRef`, which stays the same when lines move, so the fingerprint is the same across reviews and across versions of a package. A dashboard keeps one alert per finding. For an activity without an `IdRef` the label holds the line, so moving it opens a new alert.
- **Limits:** at most 25,000 results, Critical first. `runs[0].properties.truncated` says when more exist.
- Values are redacted exactly as in the index: no secret leaves through SARIF.

## `export --format cyclonedx`

A CycloneDX 1.6 JSON bill of materials:

| Element | From |
|---|---|
| `components`, type `application` | each indexed process package; `library` when it is a library |
| `components` for dependencies | every library and activity package in a `project.json`. A pinned version gets a `pkg:nuget/NAME@VERSION` purl. A version range gets the bom-ref `nuget-range:NAME@RANGE` and a `version_range` property, with no purl, because the version the robot loads is decided at runtime |
| `purl`, `hashes` | indexed `.nupkg` packages only: the SHA-256 of the package file. Source folders have the bom-ref `source:NAME@VERSION` (two checkouts of one project stay two components) and no purl |
| `properties` | `automation-index:kind`, `origin`, `layout`, `has_sources`, `coverage`, and `process` (`FOLDER/NAME`) for each deployment |
| `services` | each external host an automation reaches (`targets_url`, `calls_api`), from high- and medium-confidence edges, with the bom-ref `host:HOST` |
| `dependencies` | package to library, activity package and host, for indexed packages only. An indexed library that matches a pinned version, ignoring case as NuGet does, shares its ref, so the graph links through it. Libraries and activity packages that were never opened get no entry: their own dependencies are unknown, and an empty entry would claim they have none |
| `compositions` | an `incomplete` aggregate for the packages that were not fully read (their dependencies on hosts may be missing), and an `unknown` aggregate for the dependencies that were never opened |

The output validates against the official CycloneDX 1.6 schema.
