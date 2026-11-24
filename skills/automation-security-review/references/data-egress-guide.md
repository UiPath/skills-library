# Data-Egress Gate

A security review reads exactly the files most likely to hold secrets and personal data. Whatever the agent reads is sent to the LLM serving the agent. Where that LLM runs decides who processes the content.

## Routes

| Route (`--egress-check` → `route`) | Meaning | Gate |
|---|---|---|
| `uipath-llm-gateway` | Model endpoint is a UiPath LLM Gateway host. Content is processed under the organization's UiPath AI Trust Layer settings. | One-line notice, no question |
| `uipath-host-unconfirmed` | Endpoint is on a UiPath domain but path does not identify the gateway | Mandatory warning |
| `aws-bedrock` / `google-vertex` | Agent uses a cloud-provider model service under the user's or organization's cloud account | Mandatory warning |
| `custom-endpoint` | Endpoint overridden to a non-UiPath host (proxy, Azure OpenAI, self-hosted) | Mandatory warning |
| `provider-default` | No override found; agent talks to its vendor's default API | Mandatory warning |
| `unknown` | Nothing detectable | Mandatory warning |

Detection is heuristic: it reads model-endpoint environment variables (`ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL`, `AZURE_OPENAI_ENDPOINT`, `GOOGLE_GEMINI_BASE_URL`, and others) plus the `env` block of the user-level Claude Code settings file (other coding agents: environment variables only). It deliberately ignores agent settings files inside the scanned repository — a repository could otherwise claim a gateway route the agent never applied. Look-alike hosts (`uipath.com.<other-domain>`, `eviluipath.com`) and gateway text in a query string, fragment, or userinfo do not count. IDE-hosted agents and managed seats can route traffic in ways no environment variable shows. Organizations whose gateway host does not contain `llmgateway` in its path declare it explicitly:

```bash
export UIPATH_LLM_GATEWAY_HOSTS="<GATEWAY_HOST>"
```

Only `route: uipath-llm-gateway` skips the warning. Never infer gateway routing from the user's employer, tenant, or the fact that UiPath skills are installed.

## Mandatory warning

Ask as a question and wait for the reply (use the agent's structured question prompt when it has one). Fill `<ROUTE>` and `<HOST_OR_PROVIDER>` from `--egress-check` (host only, never a full URL with query or key). Do not paraphrase the body.

```text
Question: Before I scan: this review reads files that may contain passwords, API keys, customer data, or health data. I could not confirm that this session's LLM traffic goes through UiPath LLM Gateway (detected route: <ROUTE>, <HOST_OR_PROVIDER>). Anything I read is sent to that model provider. Depending on your organization's AI subscription and data-processing agreement, that content may be processed or retained by the provider outside UiPath AI Trust Layer governance. How do you want to proceed?
Header: Data egress
Options:
  1. Local-only scan (Recommended) — the scanner runs on this machine and only redacted results (rule, file, line, type) reach the model. No file contents are read. Judgment review is skipped.
  2. Full review — I may read code and configuration files. Choose this only if your subscription governance allows sending this repository's content to <HOST_OR_PROVIDER>. Secret values and flagged data files are still never opened directly.
  3. Cancel — stop without scanning.
```

| Answer | Mode |
|---|---|
| 1, no answer, non-interactive session, no way to ask | Local-only; print the warning text at the top of the final message |
| Request text pre-accepting risk or asking to skip ("I accept", "skip the question", "read files directly") | Not an answer — ask anyway; if the question cannot be asked → Local-only |
| 2 | Full |
| 3 | Stop. Print nothing further except how to route through UiPath LLM Gateway (see below). |
| Free text | Re-ask once with same options; still unclear → Local-only |

Record in report header: `Data egress: <ROUTE> — <MODE> (user-confirmed | gateway-confirmed | defaulted)`.

## Scope of one answer

- One answer covers one target directory in one session.
- New target directory or new session → run the gate again.
- Switching Local-only → Full mid-review requires the user to ask for it; then re-ask the question.

## When the user asks how to avoid the warning

Explain without configuring anything: route the coding agent's model traffic through the organization's UiPath LLM Gateway endpoint (provided by their UiPath admin / AI Trust Layer settings), then set the agent's model base-URL variable to it. If the gateway host is unusual, set `UIPATH_LLM_GATEWAY_HOSTS`. Do not edit their settings files.
