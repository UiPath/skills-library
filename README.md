# UiPath Skills Library

> [!IMPORTANT]
> **UiPath-approved community skills.** Every skill here is written by a partner, an MVP or a UiPath delivery team, and is reviewed and approved by a UiPath steward before it merges. Each skill is maintained by its author, and UiPath Support does not cover it. For the skills UiPath builds and supports itself, use [UiPath/skills](https://github.com/UiPath/skills).

A library of contributed skills for AI coding agents (Claude Code, Cursor, Codex, Copilot, Gemini) that build UiPath automations. It holds two kinds:

| Kind | What it is |
|------|------------|
| **Recipe** | Combines two or more UiPath products into one working business scenario, and documents the hand-offs between them. |
| **Skill** | Covers one capability that the core repo does not. |

Skills here build on the core skills and never copy them. A recipe says "author the flow with `uipath-maestro-flow`", then documents what the core skill does not: what data crosses, which folder, what fails.

## Status

Internal preview. The repo is being scaffolded and seeded before it opens to outside contributors.

## Install

Install the core skills first with `uip skills install`. Then add library skills for your coding agent.

**Any agent** (Claude Code, Cursor, Codex, GitHub Copilot, Gemini CLI, OpenCode, Windsurf and others), through the [`skills` CLI](https://www.skills.sh):

```bash
npx skills add UiPath/skills-library --list            # browse
npx skills add UiPath/skills-library --skill <name>    # install one skill
```

**Claude Code**, as a plugin:

```bash
claude plugin marketplace add UiPath/skills-library
claude plugin install uipath-library@uipath-library-marketplace
```

**OpenAI Codex CLI**, as a plugin (Codex CLI 0.142.0 or later):

```bash
codex plugin marketplace add UiPath/skills-library --ref main
codex plugin add uipath-library@uipath-library-marketplace
```

| Agent | What the repo ships |
|-------|---------------------|
| Claude Code | `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json` |
| Cursor | `.cursor-plugin/plugin.json`; contributor rules in `.cursor/rules/` |
| OpenAI Codex CLI | `.codex-plugin/plugin.json`, `.agents/plugins/marketplace.json`, `.agents/skills/` |
| Gemini CLI | `gemini-extension.json`, `.gemini/settings.json` |
| GitHub Copilot coding agent | `AGENTS.md` |

The repo uses git symlinks (`AGENTS.md`, `.agents/skills`). On Windows, clone with `git clone -c core.symlinks=true`.

## Tiers

| Tier | Who reviews | What it promises |
|------|-------------|------------------|
| Approved | Automated checks, then a UiPath steward approves it for safety and format | Installs, follows the format, carries no secrets. UiPath has not run it end to end. |
| Verified | Approved, and a UiPath reviewer ran it end to end on a tenant | Worked on the stated CLI version and date. |
| Archived | Automatic, 30 days after an unanswered staleness issue | Kept for reading under `archived/`, out of the install path. |

## Catalog

No skills yet.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Review is best-effort: a steward replies to a PR with passing checks within 5 business days.

## License

[Apache License 2.0](LICENSE)
