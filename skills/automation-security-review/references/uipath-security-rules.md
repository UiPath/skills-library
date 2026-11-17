# UiPath Security Rules (Judgment)

Full mode only. Read the evidence named per rule, decide, and record reasoning in the finding. Deterministic checks belong to the scanner ([scanner-rules.md](scanner-rules.md)) and `uip` review commands — do not repeat them here.

Severity: **C** Critical (blocks deploy), **W** Warning, **I** Info.

## RPA (`.xaml`, coded `.cs`)

| ID | Sev | Check | Evidence | Fix |
|---|---|---|---|---|
| `RPA_CRED_SOURCE` | C | Every login / API call gets its credential from Get Credential, Get Secret, Windows Credential Manager, or an Integration Service connection | Read login sequences, `InitAllApplications.xaml`, HTTP Request activities | Replace literal / Config value with Credential asset |
| `RPA_SECRET_SCOPE` | W | `SecureString` read in the scope that uses it, not at process start and passed down | Trace the variable from Get Credential to its consumer | Move Get Credential next to its consumer |
| `RPA_HAND_BUILT_AUTH` | W (C if catalog connector exists) | HTTP calls to vendor APIs carrying key/bearer headers instead of an Integration Service connection | HTTP Request / `HttpClient` with `Authorization` header | Use Integration Service connector; check with `uip is connectors list --output json` |
| `RPA_EXCEPTION_LEAK` | W | Exception messages or screenshots that can contain secrets/PII logged or emailed | Catch blocks, Take Screenshot in error handlers, Send Mail on failure | Log exception type + transaction id only; restrict screenshot storage |
| `RPA_SCREENSHOT_PII` | W | Screenshots taken on screens showing PII/PHI and stored in shared folders or buckets | Take Screenshot targets, save paths | Disable or store in restricted bucket with retention |
| `RPA_QUEUE_SENSITIVE` | W | Queue item `SpecificContent` / output carrying PII, PHI, or credentials | Add Queue Item / Set Transaction Status fields | Store reference id; keep data in source system |
| `RPA_FILE_LEFTOVERS` | W | Downloaded files with sensitive content left in temp/shared paths | Download / Save / Write activities without cleanup | Delete in Finally; use per-job temp folder |
| `RPA_HARDCODED_ENV` | I | Production URLs, tenant names, or user accounts hardcoded | Strings in selectors and HTTP calls | Move to Text assets per environment |

## Agents (`agent.json`, coded `.py`)

| ID | Sev | Check | Evidence | Fix |
|---|---|---|---|---|
| `AGENT_PROMPT_SECRETS` | C | System prompt, tool descriptions, or examples contain credentials, internal URLs with tokens, or real customer data | Read prompts, tool definitions, few-shot examples | Remove; inject at runtime via tools |
| `AGENT_EVAL_REAL_DATA` | C | Eval sets or datapoints contain real PII/PHI | Scanner PII hits in `evals/`; read eval inputs only in Full mode via `--show` | Replace with synthetic datapoints |
| `AGENT_PII_GUARDRAIL` | W | Agent handling PII/PHI has a PII-detection guardrail on input/output or tool calls | Guardrails config in `agent.json` / coded guardrail registration | Add PII guardrail (block or redact) |
| `AGENT_PROMPT_INJECTION` | W | Untrusted content (emails, documents, web, tickets) reaches the prompt or tool arguments without a prompt-injection guardrail or isolation | Input schema, tools that read external content | Add prompt-injection guardrail; separate untrusted content; constrain tool arguments |
| `AGENT_TOOL_OVERREACH` | W | Tools grant write/delete/payment/send actions without human-in-the-loop or argument constraints | Tool list, process/API tools, escalation config | Add escalation (Action Center) for irreversible tools; narrow tool scope |
| `AGENT_TRACE_PII` | W | Traces/logs capture full PII inputs/outputs | `@traced` usage, logging, trace settings | Redact inputs; disable payload capture for sensitive spans |
| `AGENT_LLM_ROUTE` | I | Coded agent calls an LLM provider directly with its own key instead of UiPath LLM Gateway | Direct `openai`/`anthropic` client construction with key | Use UiPath LLM Gateway chat models (AI Trust Layer governance) |

## Coded apps, functions, API workflows

| ID | Sev | Check | Evidence | Fix |
|---|---|---|---|---|
| `APP_SECRET_IN_BUNDLE` | C | Client-side code embeds client secret, PAT, or provider key | Source referencing env vars that a bundler inlines (`VITE_*`, `NEXT_PUBLIC_*`, `REACT_APP_*`) holding secrets | Use OAuth PKCE (public client); move secret calls to a function backend |
| `APP_SCOPES` | W | Requested OAuth scopes exceed what the app calls | `app.config.json` / auth config scopes vs SDK calls | Request least scopes |
| `APP_TOKEN_STORAGE` | W | Tokens persisted in `localStorage` or logged | Auth code paths | Keep tokens in memory / SDK-managed storage |
| `FN_INPUT_VALIDATION` | W | Function / API workflow input used in queries, file paths, URLs, or shell without validation | Handler code, JavaScript activities | Validate with schema; allowlist; parameterize |
| `FN_SSRF` | W | User-supplied URL fetched server-side | HTTP calls built from input | Allowlist hosts; block private ranges |

## Platform and solution (`.uipx`, bindings, resources)

| ID | Sev | Check | Evidence | Fix |
|---|---|---|---|---|
| `PLT_ASSET_TYPE` | C | Secrets stored as Text/Integer assets instead of Credential/Secret | Solution resource definitions, bindings | Recreate as Credential/Secret asset |
| `PLT_ROBOT_PRIVILEGE` | W | Robot / external app identity requested with admin or tenant-wide roles | Solution role assignments, documented setup steps | Assign folder-scoped least-privilege roles |
| `PLT_PAT_IN_PIPELINE` | C | PATs or client secrets inside pipeline files, scripts, or docs | CI YAML, scripts, README | Use CI secret store; rotate |
| `PLT_BUCKET_EXPOSURE` | W | Storage bucket holding sensitive files shared to broad folders or without retention | Bucket resource config, folder placement | Restrict folder; set retention |

## Developer workstation artifacts

| ID | Sev | Check | Evidence | Fix |
|---|---|---|---|---|
| `DEV_LOCAL_CONFIG_SHIPPED` | C | `.env`, `.uipath/`, local settings, or `*.local.json` included in a package or solution | Pack output, `.uipx` contents, `files` lists, `.npmignore` / `.gitignore` | Exclude from packaging; rotate if shipped |
| `DEV_DEBUG_DUMPS` | W | Debug logs, HAR files, recorded HTTP sessions, exported job logs in repo | Find files `*.har`, `*.log`, `*-logs*` | Delete; purge history if they hold tokens |
| `DEV_NOTES_SECRETS` | W | READMEs, PDDs, AGENTS.md, notebooks containing credentials or customer data | Scanner hits in `.md`, `.ipynb` | Remove; reference asset names only |
