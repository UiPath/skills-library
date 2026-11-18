# Skills Library — rules for coding agents

This repo holds community-contributed skills for UiPath. Read [CONTRIBUTING.md](CONTRIBUTING.md) before changing anything under `skills/`.

## Layout

- `skills/<name>/` — one folder per skill: `SKILL.md`, `recipe.json`, optional `references/` and `assets/`.
- `templates/skill/` — starting point for a new skill. Copy it; do not edit it for one skill.
- `schemas/recipe.schema.json` — metadata contract. `scripts/validate-skills.mjs` enforces it.
- `assets/core-skill-names.json` — names taken by the core repo, [UiPath/skills](https://github.com/UiPath/skills).
- `.github/stewards.json`, `scripts/steward-command.mjs`, `docs/steward-guide.md` — who may approve, the `/approve` and `/reject` commands, and the steward process.
- `archived/` — skills removed from the install path. Never install from here.
- `.claude-plugin/`, `.cursor-plugin/`, `.codex-plugin/`, `.agents/plugins/`, `gemini-extension.json` — per-agent plugin manifests. All carry the same name (`uipath-library`) and the `package.json` version. Bump `package.json` only, then run `npm run version:sync`.
- `.github/workflows/publish.yml`, `scripts/sync-version.mjs`, `docs/release.md` — npm publishing of `@uipath/skills-library` and the release process.

## Rules

- Skill names are kebab-case, never start with `uipath-` (reserved for the core repo), and match the folder, the `SKILL.md` frontmatter `name` and `recipe.json` `name`.
- `description` in frontmatter stays under 1024 characters.
- Skills are self-contained. Refer to core skills by name; never copy their content and never link into another skill's folder.
- `SKILL.md` is tool-agnostic markdown. No tool names or slash commands that exist in only one coding agent.
- No secrets, tenant identifiers, customer names or real sample data. Use `<PLACEHOLDER>` values.
- Every `SKILL.md` body carries the line `UiPath-approved community skill. Maintained by its author.`
- Run `npm run validate` before committing.
