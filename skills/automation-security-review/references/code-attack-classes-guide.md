# Code Attack Classes — Trust-Boundary Review

Full mode only, for code that accepts input from outside the process: coded apps, Coded Functions, API workflows, Integration Service connectors, MCP servers, coded agents, webhooks.

Approach adapted from the MIT-licensed [cloudflare/security-audit-skill](https://github.com/cloudflare/security-audit-skill): map boundaries first, hunt only classes that match the code, try to disprove each candidate, report only what crosses a real trust boundary.

## 1. Map boundaries (before hunting)

List for each project:

| Item | Question |
|---|---|
| Entry points | HTTP handlers, function `main`, webhook receivers, agent input schema, MCP tools, queue triggers |
| Callers | Who can reach each entry point — public internet, tenant users, robots, other agents? |
| Untrusted data | Request bodies, query strings, headers, uploaded files, emails, documents, LLM output, queue items from external sources |
| Sinks | Database queries, file paths, shell/process start, HTTP requests, HTML rendering, LLM prompts, tool arguments, Orchestrator APIs |
| Privilege | Identity the code runs as (robot account, external app, user token) and its scopes |

A finding exists only when untrusted data reaches a sink with more privilege than the caller already has.

## 2. Hunt matching classes

| Class | Applies when | Look for |
|---|---|---|
| Injection | Input reaches SQL, shell, file paths, LDAP, XPath, template engines | String concatenation into queries/commands; `Process.Start`, `subprocess(shell=True)`, `eval`, path joins without normalization |
| Broken authentication | Code issues or validates tokens, sessions, webhooks | Missing signature verification on webhooks; JWT `alg: none`/no audience check; secrets compared non-constant-time |
| Broken authorization | Multi-user or multi-tenant data access | Object ids from input used without ownership check; folder/tenant id taken from request instead of token |
| SSRF | Server fetches URLs or hosts from input | No host allowlist; redirects followed to private ranges or cloud metadata endpoints |
| XSS / client-side | Coded apps render input or LLM output | `dangerouslySetInnerHTML`, `innerHTML`, unescaped markdown rendering |
| Prompt injection & tool abuse | Agent or LLM call sees external content | External text concatenated into system prompt; tool arguments taken verbatim from model output for write/delete/send actions; no human approval on irreversible tools |
| Sensitive data exposure | Responses, logs, traces, errors | Stack traces returned to callers; full request bodies logged; PII in trace spans |
| Supply chain | Dependencies, build, release | Unpinned dependency ranges for security-critical packages; install scripts from unknown sources; lockfile missing; `.npmrc` with tokens |
| Resource exhaustion | Public or high-volume entry points | Unbounded file uploads, pagination, recursion, regex on untrusted input; no timeout on outbound calls |
| Tenant isolation & data lifecycle | Shared storage, caches, buckets | Cache keys without tenant id; temp files not deleted; data retained beyond need |

Skip classes that do not apply; list them as not applicable only when the user asked for a full audit.

## 3. Disprove each candidate

For every candidate, try in order:

1. Is the input actually attacker-controlled given the callers in step 1?
2. Is there validation, encoding, or an allowlist upstream?
3. Does the framework neutralize it (parameterized ORM, React escaping, SDK-managed auth)?
4. Does the sink run with privilege the caller lacks?

Survives all four → `confirmed`. Unclear without running code → `needs-validation` with the exact question to answer. Never execute target code or probe live systems.

## 4. Record

Each finding: class, entry point → sink path (`file:line` chain), privilege gained, smallest fix. Defense-in-depth gaps without an exploitable path are **Info**, not vulnerabilities.
