# Review findings

The `review` command clears the `finding` table and rebuilds it from the index. Each run writes `INDEX_DIR/reports/review-DATE.md`. Every row carries the evidence it came from. No language model is involved.

| Rule | Severity | Finding | Read it as |
|---|---|---|---|
| AI-COV-001 | Warning | Package could not be read from source | Coverage gap: compiled-only, unsupported type, or download failure. Facts for it are partial |
| AI-SEC-001 | Critical | Hard-coded secret | Literal password, token or key in an activity property, connection string, Invoke Code body or Config file. Values are redacted; the file and activity are given |
| AI-RES-001 | Warning | Asset referenced in code does not exist in Orchestrator | One row per process and asset, with the number of references. When the activity names a folder (`FolderPath`), the asset must exist in that folder. Checked against every folder the crawl could see; if the crawling account can't see a folder, an existing asset looks missing. Skipped when the asset inventory could not be read |
| AI-RES-002 | Info | Asset exists, but not in the folder of the process that reads it | Possible runtime failure, or the asset is shared through another folder. Skipped when the activity sets an explicit folder |
| AI-RES-003 | Warning | Queue referenced in code does not exist in Orchestrator | Same caveat as AI-RES-001 |
| AI-RES-004 | Info | Orchestrator asset not referenced by any indexed package | Candidate for clean-up. The detail states the coverage gaps; with compiled-only packages or dynamic asset reads, "unused" is not proven |
| AI-RUN-001 | Warning | Deployed process with no runs in the job window | Orphan candidate. The window is `jobs_window_days`; if `jobs_truncated` is set, counts are lower bounds |
| AI-RUN-002 | Warning | Process failed in more than 20% of runs | At least 5 runs in the window |
| AI-LIB-001 | Info | Same library at several versions across deployed processes | Library version drift |
| AI-LIB-002 | Info | Activity package at more than two versions | Activity package drift |
| AI-SRC-001 | Info | Workflow not reachable from any entry point | Dead code. Only reported when no dynamic invoke could reach the file |
| AI-SRC-002 | Info | Target resolved only at runtime | One row per package and kind (dynamic URL, asset, queue or process name), with a count and the first occurrence |
| AI-SRC-003 | Warning | Workflow file could not be parsed | Malformed XML |
| AI-WA-001 | Warning | Workflow Analyzer errors in the deployed package | From `uip rpa analyze` against the tenant's published policy or the supplied policy file. Reported verbatim |
| AI-WA-002 | Info | Workflow Analyzer could not run on this package | Usually a Windows-target project on a non-Windows host |
| AI-LNK-001 | Info | Queue written by an automation but read by none of the indexed automations | A broken handover, or a queue read outside UiPath, through a dynamic name, or by a package that could not be read. The detail gives the count of packages not fully read |
| AI-LNK-002 | Info | Queue read by an automation but written by none of the indexed automations | Items fed from outside UiPath (API, other systems) or by an automation that is not indexed |
| AI-GOV-001 | Critical | Application or URL outside the restriction list | Only with `--restrictions FILE` |

## Restriction file

This is a static approximation of the Automation Ops App/Url restriction rules. It is not the policy engine itself, so say so when reporting.

```json
{
  "applications": { "allow": ["msedge.exe", "chrome.exe", "excel.exe", "saplogon.exe"], "deny": [] },
  "urls": { "allow": ["*.example.com", "login.example.net"], "deny": ["*.pastebin.com"] }
}
```

- Patterns are shell globs, case-insensitive. Applications are matched on the executable name. URLs are matched on the host and on the full URL.
- `deny` always wins. When `allow` is present, anything not matching it is reported. Leave `allow` out to check against the deny list only.
- Dynamic values are not checked. Count them with `SELECT count(*) FROM edge WHERE kind IN ('targets_url','calls_api','uses_application') AND normalized='<dynamic>'` and report the number next to the result.

## Workflow Analyzer evidence for a governance question

AI-WA-001 is the deterministic answer to "did a package reach Orchestrator without passing our rules?". It re-runs the analyzer on exactly what is deployed, not on what the developer says they ran. Two limits:

1. `uip rpa analyze` refuses Windows-target and Legacy projects on macOS and Linux (`Cannot execute Windows projects on Linux platform`). Run the `analyze` step on a Windows host for those packages, or report them as AI-WA-002.
2. Compiled-only packages have no source to analyze.
