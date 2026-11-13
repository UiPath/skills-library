# Contributing to the UiPath Skills Library

Thanks for sharing what you learned on real projects. This guide is the whole process.

## What belongs here

| Kind | Belongs when | Extra requirement |
|------|--------------|-------------------|
| **Recipe** | It combines two or more UiPath products into one working scenario | `references/seams-guide.md` with the hand-off table |
| **Skill** | It covers one capability no core skill covers | None |

Does not belong here: changes to a core skill (names starting with `uipath-`). Open those in [UiPath/skills](https://github.com/UiPath/skills). Solution code, `.uipx` packages and real sample data do not belong either; a skill is instructions plus placeholder assets.

## Add a skill

1. Copy `templates/skill/` to `skills/<your-skill-name>/`.
2. Pick the name: kebab-case, not starting with `uipath-`, not a name the core repo uses. Use the same name for the folder, the `name` in `SKILL.md` frontmatter and the `name` in `recipe.json`.
3. Write `SKILL.md`. Keep the frontmatter `description` under 1024 characters and lead with the scenario and products. Keep the line `UiPath-approved community skill. Maintained by its author.` in the body.
4. Fill in `recipe.json`:

   | Field | Value |
   |-------|-------|
   | `kind` | `recipe` or `skill` |
   | `authors` | Your GitHub login (no `@`), plus optional name and organization |
   | `products` | UiPath products the skill touches; at least two for a recipe |
   | `tier` | `community`. A steward sets `verified` |
   | `cli_version` | The `uip` version you ran it on |
   | `industry`, `upstream` | Optional |

5. For a recipe, fill in `references/seams-guide.md`: one row per hand-off between products, with the data that crosses and the failure you hit.
6. Run the checks, then open a pull request:

   ```bash
   npm run validate
   git commit -s -m "Add <your-skill-name>"
   ```

## Rules for skill content

- **Build on the core skills.** Name the core skill that does a step (`uipath-maestro-flow`, `uipath-rpa`). Do not copy its content and do not link into another skill's folder.
- **No real data.** No secrets, tenant names, folder keys, connection IDs or customer names. Use placeholders such as `<FOLDER_NAME>`.
- **Tool-agnostic.** Skills run in Claude Code, Cursor, Codex, Copilot and Gemini. Do not rely on tool names or slash commands from one of them.
- **Say what fails.** The known failures you hit on a real project are the most useful part.

## What happens to your pull request

1. Automated checks run. If one fails, its output says what to fix.
2. When checks pass, a steward reviews for safety and format. Expect a reply within 5 business days. Review is best-effort.
3. The steward approves, or declines with a reason. A merged skill is listed as UiPath approved. A commit pushed after approval needs a new approval.
4. On merge you become the owner of your skill folder in `CODEOWNERS`.

## For stewards

Stewards approve with `/approve <commit>` and decline with `/reject <reason>`, typed as a pull request comment. The full process is in the [steward guide](docs/steward-guide.md).

## Owning a skill

You own what you contribute. When the `uip` CLI changes and your skill breaks, an issue is opened and assigned to you. A skill whose owner does not reply within 30 days moves to `archived/`: still readable, no longer installable. You can bring it back with a pull request.

## Sign-off and license

Contributions are licensed under [MIT](LICENSE). Sign off each commit with `git commit -s` to certify the [Developer Certificate of Origin](https://developercertificate.org/): you wrote it, or you have the right to submit it.

## Conduct and security

Follow the [Code of Conduct](CODE_OF_CONDUCT.md). Report vulnerabilities privately, as described in [SECURITY.md](SECURITY.md).
