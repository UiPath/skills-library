# Read-only operation

## Commands the indexer runs

These are the only `uip` commands in `scripts/automation-index.mjs`. All of them are reads. They are started with an argument list, never through a shell, because arguments include names that come from Orchestrator.

| Command | Why |
|---|---|
| `uip login status` | Confirm a session exists. The indexer never logs in |
| `uip or folders list --all` | Folder tree |
| `uip or processes list --all-folders --all-fields` | Deployed processes, package versions, feed ids |
| `uip or assets list --folder-key K` | Asset names, types and scope. Not values |
| `uip or queues list`, `uip or buckets list`, `uip or triggers list --type T`, `uip or machines list` | Resource inventory |
| `uip or jobs list --all-folders --all-fields --created-after D` | Aggregated into run counts and identities. Job payloads are not stored |
| `uip or jobs get KEY --no-download` | Only with `--resolve-identities`: one call per distinct identity, to read the Orchestrator robot identity. `--no-download` stops the CLI from saving the job's output file to the current directory |
| `uip or packages download ID:VERSION --feed-id F` | The deployed package, from the feed the process uses, including folder feeds |
| `uip or feeds list`, `uip or libraries download ID:VERSION` | Custom libraries pinned by deployed packages |
| `uip rpa analyze COPY_DIR` | Only in the `analyze` step, on a throwaway copy |

`uip rpa analyze` restores NuGet dependencies, writes into the project's `.local/` folder, and packs the project internally to validate it. It publishes nothing. The indexer runs it on a temporary copy under `INDEX_DIR/work/` and deletes the copy afterwards.

## Commands the agent must not run while using this skill

- Anything that changes Orchestrator: `create`, `update`, `delete`, `start`, `stop`, `restart`, `resume`, `share`, `unshare`, `assign`, `upload`, `publish`, `deploy`, `rollback`, `update-version`
- `uip or assets get-asset-value`: asset values stay in Orchestrator
- `uip rpa build`, `pack`, `publish`, `run`, `debug`, `remote`, `cleanup` (without `--dry-run`), `uip solution` lifecycle commands
- `uip login`: a human logs in

## Making read-only enforceable

A skill is guidance, and an agent can step outside it. Enforce read-only outside the skill:

1. **Credentials.** Crawl with an Orchestrator external application or account that has view permissions only (folders, processes, packages, assets, queues, buckets, triggers, machines, jobs). With no write permission, a mutating command fails at the server whatever the agent does.
2. **No credentials for question-answering.** Run `crawl` on a schedule from a service account. Give analysts and agents only `INDEX_DIR`. `extract`, `review`, `cards`, `query` and `status` need no login.
3. **Agent permission settings.** Most coding agents can deny shell commands by pattern. Deny the commands listed above and allow `node .../automation-index.mjs` and read-only `uip or ... list|get` commands.
4. **The `query` command** accepts exactly one `SELECT`, `WITH`, `VALUES` or `EXPLAIN` statement, or a `PRAGMA` from a read-only list (`table_info`, `table_list`, `index_list` and similar). It opens the database read-only, with `PRAGMA query_only` and in-memory temporary storage. `VACUUM INTO`, `ATTACH`, checkpoints, multiple statements and writes are refused.

## What the index contains

- `index.db`: facts, names of Orchestrator objects, account names that ran jobs, and Config values with credential-like values redacted.
- `src/`: an unmodified copy of each package's project tree. It contains whatever the source contains, including any hard-coded secrets that AI-SEC-001 reports.
- `packages/`: the downloaded `.nupkg` files.

Treat `INDEX_DIR` like the source repository: same access controls, same retention. Do not paste rows from it into external services.

The script refuses to write through symbolic links inside `INDEX_DIR` (`src/`, `packages/`, `work/`, `reports/`, `cards/`), and never reads `INDEX_DIR` back as source when it sits inside a source tree.

## When a crawl partly fails

Each inventory (processes, assets, queues, buckets, triggers, jobs) is replaced only when all of its list calls succeed. On a failure the previous rows stay, `status` lists the error, and `meta.inventory_<type>_ok = 0` makes the review rules that depend on it skip themselves rather than report everything as missing.
