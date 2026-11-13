# UiPath Skills Library

> [!IMPORTANT]
> **Community skills. Not supported by UiPath.** Skills here are written by partners, MVPs and UiPath delivery teams. They are checked for format and safety, not tested by UiPath unless a skill is marked Verified. For the supported per-product skills, use [UiPath/skills](https://github.com/UiPath/skills).

A library of contributed skills for AI coding agents (Claude Code, Cursor, Codex, Copilot, Gemini) that build UiPath automations. It holds two kinds:

| Kind | What it is |
|------|------------|
| **Recipe** | Combines two or more UiPath products into one working business scenario, and documents the hand-offs between them. |
| **Skill** | Covers one capability that the core repo does not. |

Skills here build on the core skills and never copy them. A recipe says "author the flow with `uipath-maestro-flow`", then documents what the core skill does not: what data crosses, which folder, what fails.

## Status

Internal preview. The repo is being scaffolded and seeded before it opens to outside contributors.

## Install

```bash
npx skills add UiPath/skills-library --list            # browse
npx skills add UiPath/skills-library --skill <name>    # install one skill
```

Install the core skills first with `uip skills install`.

## Tiers

| Tier | Who reviews | What it promises |
|------|-------------|------------------|
| Community | Bots, plus a steward skim for safety and format | Installs, follows the format, carries no secrets. Not tested by UiPath. |
| Verified | A UiPath reviewer ran it end to end on a tenant | Worked on the stated CLI version and date. |
| Archived | Automatic, 30 days after an unanswered staleness issue | Kept for reading under `archived/`, out of the install path. |

## Catalog

No skills yet.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Review is best-effort: a steward replies to a PR with passing checks within 5 business days.

## License

[MIT](LICENSE)
