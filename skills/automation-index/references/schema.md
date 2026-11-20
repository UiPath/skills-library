# Index schema and example queries

The index is one SQLite file, `INDEX_DIR/index.db`. Query it only with the `query` command, which opens it read-only.

```bash
node SKILL_DIR/scripts/automation-index.mjs --index-dir INDEX_DIR query "SELECT ..." [--format table|json|csv] [--limit N]
```

## Tables

### From Orchestrator (the `crawl` command)

| Table | One row per | Key columns |
|---|---|---|
| `folder` | folder, including personal workspaces and solution folders found through processes | `key`, `path`, `type` |
| `process` | deployed process (a package bound to a folder) | `key` (upper-case GUID), `name`, `folder_path`, `package_id`, `version`, `process_type`, `target_framework`, `feed_id` |
| `orch_asset` | asset in a folder. **Names, types and scope only. Values are never read.** | `name`, `folder_path`, `value_type`, `value_scope` |
| `orch_queue`, `orch_bucket` | queue or storage bucket in a folder | `name`, `folder_path` |
| `orch_trigger` | time, queue or API trigger | `trigger_type`, `name`, `process_name`, `process_key`, `enabled` |
| `orch_machine` | machine template | `name`, `type`, `scope` |
| `job_summary` | process × folder in the job window | `runs`, `runs_30d`, `faulted`, `last_run`, `last_success`, `window_days` |
| `job_identity` | process × local account × host machine | `local_account`, `host_machine`, `orchestrator_identity` (only with `--resolve-identities`), `runs` |

`job_summary.process_key = ''` means jobs with no process (Studio debug runs, deleted processes).

### From packages (the `extract` command)

| Table | One row per | Key columns |
|---|---|---|
| `package` | package version read, or project folder read | `package_id`, `version` (the package version, or `source:PATH` for a project folder), `kind` (process/library/other), `origin` (download/supplied/source), `layout` (modern/legacy/source/compiled), `has_sources`, `target_framework`, `studio_version`, `sha256`, `solution` (folder of the enclosing `.uipx`, for sources), `file_path` |
| `coverage` | package × stage | `stage` (download/extract), `status` (ok/indexed/compiled_only/unsupported/parse_error/download_failed), `detail` |
| `dependency` | package dependency | `dep_name`, `dep_spec`, `dep_version` (exact when pinned, else NULL), `is_activity_pack` |
| `entry_point` | entry point | `path`, `source` |
| `workflow` | `.xaml`/`.cs` file, or a workflow name recovered from a compiled DLL | `path`, `kind` (xaml/cs/compiled), `activities`, `reachable` (1 yes, 0 no, NULL unknown), `parse_error` |
| `invocation` | Invoke Workflow / Invoke Process call | `from_path`, `line`, `kind`, `target_raw`, `target_path`, `static` |
| `edge` | fact: the automation touches something | see below |
| `config_key` | Config.xlsx / config JSON key | `sheet`, `name`, `value` (credential-like values redacted), `redacted` |
| `binding` | resource in `bindings_v2.json` | `resource`, `name`, `folder_path` |

### From analysis and review

| Table | Contents |
|---|---|
| `analyzer_run` | one row per package: `status` (ok/failed/skipped_platform/skipped_no_source_tree), counts, policy |
| `analyzer_finding` | Workflow Analyzer output, verbatim: `level`, `rule`, `file`, `activity_id`, `description` |
| `finding` | review findings: `rule`, `severity`, `subject`, `detail`, `evidence`, `process_key` |
| `ai_summary` | summaries written by an agent: `model`, `text`, `source_sha256`. Non-authoritative. |
| `meta` | `crawl_finished`, `crawl_tenant`, `jobs_window_days`, `jobs_truncated`, `crawl_errors`, tool versions |

## The `edge` table

| Column | Meaning |
|---|---|
| `kind` | what is touched (list below) |
| `value` | the value as written, or as resolved |
| `normalized` | the comparison key: lower-case host for URLs, lower-case executable for apps, `<dynamic>` when unknown until runtime |
| `detail` | how it was found, for example `TargetApp.Url`, `selector <html>`, `via Config key 'X'`, `traced <- argument ...` |
| `static` | 1 when the value is written literally in the source, 0 when resolved or dynamic |
| `file`, `line`, `activity` | where it was found. `activity` is the Studio IdRef plus the display name |
| `extractor` | what found it (see [extractors.md](extractors.md)) |
| `confidence` | `high`, `medium` or `low` |

There is one row per distinct value per activity. Where there is no activity (coded workflow lines, Config rows, DLL strings), there is one row per value per line. Values are scrubbed of credential material before they are stored.

Edge kinds: `uses_application`, `targets_url`, `calls_api`, `reads_asset`, `writes_asset`, `reads_credential`, `writes_credential`, `uses_queue` (detail starts with `add` or `get`), `uses_bucket`, `invokes_process`, `uses_connection` (Integration Service), `uses_database`, `sends_email_to_domain`, `uses_mail_server`, `runs_code`, `ui_technology` (selector root tag: `wnd`, `html`, `sap`, `java`, `uia`, `citrix`...), `window_title`, `uses_file`, `uses_config_key`, `uses_or_descriptor`, `hardcoded_secret` (value always redacted).

## Views

| View | Use it for |
|---|---|
| `v_automation` | one row per deployed process with package, sources flag, runs, failures and coverage |
| `v_edge_all` | every edge of a package **including edges inherited from its libraries**; `via` shows the library path, empty for direct edges |
| `v_process_edge` | `v_edge_all` joined to deployed processes: the usual starting point for a question |
| `v_dep_closure` | transitive library dependencies (pinned versions only) |

## Example queries

Which applications do our robots use, and how many automations use each?

```sql
SELECT normalized AS application, count(DISTINCT process_key) AS automations,
       group_concat(DISTINCT extractor) AS found_by, min(confidence) AS weakest
FROM v_process_edge WHERE kind = 'uses_application'
GROUP BY normalized ORDER BY automations DESC;
```

Which hosts do they reach, by browser or by API?

```sql
SELECT normalized AS host, kind, count(DISTINCT process_key) AS automations
FROM v_process_edge WHERE kind IN ('targets_url', 'calls_api') AND normalized <> '<dynamic>'
GROUP BY normalized, kind ORDER BY automations DESC;
```

How many robot accounts run a given automation?

```sql
SELECT p.name, p.folder_path, count(DISTINCT j.local_account) AS accounts,
       group_concat(DISTINCT j.local_account) AS account_list, sum(j.runs) AS runs
FROM process p JOIN job_identity j ON j.process_key = p.key
WHERE p.name LIKE '%PROCESS_NAME%' GROUP BY p.key;
```

Impact of a change: which automations touch an application or host, through which file and activity, including through libraries?

```sql
SELECT process_name, folder_path, kind, value, file, line, activity, via, confidence
FROM v_process_edge
WHERE normalized IN ('APP_EXE_LOWERCASE', 'HOST_LOWERCASE')
ORDER BY process_name, file, line;
```

Which processes read an asset or credential?

```sql
SELECT DISTINCT process_name, folder_path, kind, value, extractor, file, line
FROM v_process_edge WHERE kind IN ('reads_asset', 'reads_credential') AND normalized = lower('ASSET_NAME');
```

Which processes use library X, and at which version?

```sql
SELECT p.name, p.folder_path, d.dep_spec FROM process p
JOIN dependency d ON d.package_id = p.package_id AND d.version = p.version
WHERE d.dep_name = 'LIBRARY_ID' ORDER BY d.dep_spec;
```

Projects per solution, for an index built from a repository:

```sql
SELECT COALESCE(solution, '(no solution)') AS solution, count(*) AS projects,
       group_concat(package_id, ', ') AS names
FROM package WHERE origin = 'source' AND kind <> 'other' GROUP BY solution;
```

What is unknown? Run this before you answer "none" or "all":

```sql
SELECT stage, status, count(*) FROM coverage GROUP BY stage, status;
SELECT kind, count(*) FROM edge WHERE normalized = '<dynamic>' GROUP BY kind;
```
