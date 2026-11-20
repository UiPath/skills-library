---
name: automation-index
description: "Builds and queries a read-only, evidence-backed index of every UiPath automation deployed in an Orchestrator tenant, or in a folder of .nupkg packages or project sources. Records which applications, URLs, APIs, assets, credentials, queues, buckets, databases, libraries and robot accounts each automation uses, with file and line, and reviews them for hard-coded secrets, missing or unused assets, processes that never run, library drift, Workflow Analyzer violations and apps or URLs outside an allow list. Use when asked what applications, URLs or robot accounts our robots use, which processes read an asset or queue, what breaks if an application, website, login page or library changes, to inventory, audit or document many automations at once, or to check what is deployed against governance rules. Never builds, packs, publishes, runs or changes anything. For one project's code quality use uipath-review; to change an automation use uipath-rpa; for Orchestrator operations use uipath-platform."
compatibility: "Node.js 22.5 or later, the same Node the uip CLI needs; no npm install. The crawl and analyze steps also need the uip CLI and a logged-in, ideally read-only, Orchestrator session."
license: MIT
---

# Automation Index

> UiPath-approved community skill. Maintained by its author.

Platform and CoE teams with hundreds of automations get questions like these: which applications our robots use, which URLs they reach, how many robot accounts run a process, what breaks when an application's login page changes next week. This skill answers them from a deterministic index, not from reading packages one by one. A bundled script reads Orchestrator and every deployed package with a real XML parser and writes each fact to SQLite with its file, line, activity and extractor. The agent answers from SQL, falls back to the extracted source with `file:line` citations, and reports what it could not see.

## Critical Rules

1. **Read-only.** Use only the commands in [references/read-only-operation.md](references/read-only-operation.md). Never create, update, delete, start, publish, pack, build, run or deploy anything. Never read asset values. Never run `uip login`; a human logs in.
2. **Counts come from SQL.** Every number, list and mapping in an answer comes from a `query` result. Do not count by reading files.
3. **Cite evidence.** Each fact you report carries its `file:line` or `activity`, its extractor and its confidence. Present `low` confidence rows as hints, never as facts.
4. **Report coverage with every answer.** Before you say "none", "all" or give a total, check `coverage` and dynamic edges (queries at the end of [references/schema.md](references/schema.md)). Say how many automations were compiled-only, unsupported or not downloaded, and how many values are dynamic.
5. **Keep AI text apart.** Summaries you write go in through `summary set` and are shown as non-authoritative. Never edit `index.db` any other way.
6. **The index holds source code.** `INDEX_DIR/src/` copies the customer's packages, including any secrets in them. Do not paste its contents or the database rows into external services.

## Prerequisites

- Node.js 22.5 or later, which the `uip` CLI already requires. The script uses only Node built-ins, so there is nothing to install.
- For a tenant crawl: the `uip` CLI, with a human-run `uip login`, ideally as an account or external app with view permissions only. The core skill `uipath-platform` covers login and profiles.
- Without a login you can still index packages or source folders someone gives you.

`SKILL_DIR` below is the folder that holds this file. `INDEX_DIR` is where the index lives; it defaults to `./automation-index`. `node SKILL_DIR/scripts/automation-index.mjs --help` lists every command and option.

## Instructions

### Step 1: Pick the input

| The user has | Run |
|---|---|
| A logged-in tenant | Step 2, then Step 3 |
| One `.nupkg`, or a folder of them (exported, or from a CI artifact store) | Step 3 with `--nupkg PATH` (a file, or a folder searched recursively) |
| One project folder, a repository, a solution, or a monorepo of solutions | Step 3 with `--source PATH` |

`--source` and `--nupkg` can be repeated and combined with a crawl in one index. `--source` indexes every folder that contains a `project.json`, at any depth:

- **Solutions:** the nearest folder holding a `.uipx` file is recorded in `package.solution`.
- **Projects with the same name**, for example copies or branches checked out side by side, stay separate. Their version is `source:` followed by the path, such as `source:repo/SolutionB/Invoices`.
- **A project nested inside another project** is indexed on its own and is not counted as part of the outer one.
- **Libraries indexed from source** pass their facts to the projects that depend on them, when the exact pinned version is not indexed.
- **Agent, Flow, BPMN, app and API-workflow projects** (no `project.json`) are listed in `status` as unsupported, not skipped silently.

If an index already exists, run `status` first and reuse it. Re-crawl only when the user needs current data. Check `crawl_finished` in the status output.

### Step 2: Crawl Orchestrator

```bash
node SKILL_DIR/scripts/automation-index.mjs --index-dir INDEX_DIR crawl --jobs-days 90 --resolve-identities
```

This reads folders, processes, assets (names only), queues, buckets, triggers, machines and jobs from the last 90 days. It then downloads the deployed version of each package from that process's own feed, plus the custom libraries those packages pin. Expected output ends with `crawl done: N processes, M folders, E errors`. If E is not 0, run `status` and read the crawl errors before you go on. Reading many jobs is slow; `--max-jobs` caps it, and the cap is recorded.

### Step 3: Extract

```bash
node SKILL_DIR/scripts/automation-index.mjs --index-dir INDEX_DIR extract [--nupkg PATH] [--source PATH]
```

Expected output: one `indexed PACKAGE@VERSION` line per package, then `extract done: N packages read, F failed.` Packages already indexed with the same hash are skipped; `--force` re-reads them.

### Step 4: Check coverage

```bash
node SKILL_DIR/scripts/automation-index.mjs --index-dir INDEX_DIR status
```

Read the "Not fully read" list. Packages marked `partial` had an unreadable `project.json`. Packages marked `compiled_only` were published without sources. Their facts come only from bindings, Studio's application metadata and DLL strings. If many automations are compiled-only, tell the user, and ask whether a source repository exists that you can index with `--source`.

### Step 5 (optional): Run Workflow Analyzer on what is deployed

```bash
node SKILL_DIR/scripts/automation-index.mjs --index-dir INDEX_DIR analyze [--policy-file FILE] [--governance-product Development]
```

This runs `uip rpa analyze` on a throwaway copy of each package that has sources, against the tenant's published Automation Ops policy or the supplied policy file. Windows-target and Legacy packages are recorded as `skipped_platform` on macOS and Linux. Run this step on a Windows host to cover them.

### Step 6: Review and cards

```bash
node SKILL_DIR/scripts/automation-index.mjs --index-dir INDEX_DIR review [--restrictions FILE]
node SKILL_DIR/scripts/automation-index.mjs --index-dir INDEX_DIR cards
```

`review` prints the path of `reports/review-DATE.md`. `cards` writes `INDEX.md` and one Markdown card per automation in `cards/`. Rules and the restriction file format are in [references/findings.md](references/findings.md).

### Step 7: Answer questions

Work in this order:

1. **Query.** Open [references/schema.md](references/schema.md) for tables, views and example SQL. Start from `v_process_edge`, which includes edges inherited from libraries.

   ```bash
   node SKILL_DIR/scripts/automation-index.mjs --index-dir INDEX_DIR query "SELECT ..." --format table
   ```

2. **Read the source.** If the database lacks the fact, search the package's source tree (`INDEX_DIR/src/PACKAGE@VERSION_HASH/`; the card's Provenance section gives the exact folder) and cite `file:line`. Cards (`INDEX_DIR/cards/`) are a quick way to understand a single automation.
3. **Propose an extractor.** If the same question will come back, say which activity and property an extractor should read. Do not change the script during an answer.

Shape of an answer: the result, the SQL that produced it, evidence for the top rows, and a coverage line, for example "312 of 340 automations read from source; 25 compiled-only; 3 not downloaded; 41 URLs built at runtime".

### Step 8 (optional): Summaries

When asked to describe what an automation does, read its card and source tree and write 3–5 lines. Then review them as a sceptic would: split them into claims and try to disprove each one against the index, following [references/summary-review.md](references/summary-review.md). Correct or drop every claim that is not supported, and store the summary with its review:

```bash
node SKILL_DIR/scripts/automation-index.mjs --index-dir INDEX_DIR summary set --package ID --version V --model MODEL_NAME --file summary.md --review review.md
```

`summary set` refuses a summary without a review, or with any claim marked `UNSUPPORTED` or `CONTRADICTED`.

Cards show the summary under "AI summary (non-authoritative)" with the number of claims checked, and mark it stale when the package changes.

### Step 9 (optional): Hand the index to another tool

```bash
node SKILL_DIR/scripts/automation-index.mjs --index-dir INDEX_DIR export --out fingerprints.jsonl
```

This writes one JSON fingerprint per package (schema `automation-index/fingerprint@1`). Each fingerprint carries identity, framework, activity counts, complexity signals (decisions, try/catch, retries, loops, human tasks), applications, hosts, assets, queues written and read, processes called, libraries, upstream and downstream automations, findings, and the reviewed summary marked non-authoritative. Discovery or migration tools can read it instead of parsing packages again.

## Examples

**Example 1: inventory question**
User says: "What applications and URLs do our robots use?"
Actions: `status` for coverage; query applications and hosts grouped by automation count (examples in schema.md); list the top rows with one evidence row each.
Result: two ranked tables plus a coverage line. Low-confidence rows from compiled packages are listed separately.

**Example 2: change impact**
User says: "The supplier portal login page changes next week. Which automations break?"
Actions: find the portal's host and executable in `edge`; query `v_process_edge` for those normalized values, including `via` for library paths; follow `v_process_link` downstream to the automations they feed through queues or process calls; add `job_summary` to show which ones actually run.
Result: affected automations with folder, file, line and activity of every selector, URL or library involved, plus the automations downstream of them, sorted by run volume.

**Example 3: robot accounts**
User says: "How many robot accounts does the Invoice Dispatcher use?"
Actions: query `job_identity` for the process (crawl must have run with jobs; `--resolve-identities` adds the Orchestrator identity).
Result: count and list of accounts and machines in the job window, with the window stated.

**Example 4: governance check**
User says: "Did anything reach production without passing our analyzer rules or using unapproved apps?"
Actions: `analyze` with the policy, `review --restrictions FILE`, then report AI-WA-001, AI-WA-002 and AI-GOV-001.
Result: violations with evidence, plus the packages that could not be analyzed and why.

## Troubleshooting

**Error:** `Not logged in. A human runs uip login ...`
**Cause:** no Orchestrator session. **Solution:** ask the user to log in themselves; the skill never logs in. Or index supplied packages or sources instead.

**Error:** `uip CLI not found on PATH.`
**Cause:** the CLI is not installed or not on PATH. **Solution:** pass `--uip PATH` or install the CLI (see `uipath-platform`).

**Symptom:** most packages are `compiled_only`.
**Cause:** they were published without sources, which is common for Studio Web and cross-platform publishing. **Solution:** say so in every answer, and ask for the source repositories (`extract --source`).

**Symptom:** `analyze` shows `skipped_platform`.
**Cause:** `uip rpa analyze` refuses Windows-target and Legacy projects on macOS and Linux. **Solution:** run the `analyze` step on Windows against the same `INDEX_DIR`.

**Symptom:** `download_failed` for a process or library.
**Cause:** the package is in a feed the account cannot read, or the library version is a range. **Solution:** read `status`; grant feed access, or supply the `.nupkg` with `--nupkg`.

**Symptom:** an asset shows as missing (AI-RES-001) but exists.
**Cause:** the crawling account cannot see that folder. **Solution:** check the account's folder access before reporting it as missing.

**Error:** `query is read-only: ... is not allowed` or `query runs exactly one statement`.
**Cause:** `query` accepts one `SELECT`, `WITH`, `VALUES`, `EXPLAIN` or read-only `PRAGMA` statement. **Solution:** this is by design; the index changes only through the script's commands.

**Error:** `automation-index needs Node.js 22.5 or later`.
**Cause:** an older Node is first on PATH. **Solution:** use the Node that runs the `uip` CLI (22 or later).

Extractor details and known blind spots: [references/extractors.md](references/extractors.md).
