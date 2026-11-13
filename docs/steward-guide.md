# Steward Guide

A steward decides whether a contributed skill joins the library. Once you approve a skill it is listed as **UiPath approved**, so your review is the quality bar.

Stewards are the people in [`.github/stewards.json`](../.github/stewards.json), who are also the `@UiPath/skills-steward` team.

## The job in one minute

1. Open a pull request whose `Validate skills` check is green.
2. Read the skill as the instructions an agent will follow.
3. Comment `/approve` to merge it, or `/reject <reason>` to decline it.

Contributors are promised a reply within 5 business days of their checks passing. A clear "no" within a week is better than silence.

## Find pull requests to review

- [Open pull requests, oldest first](https://github.com/UiPath/skills-library/pulls?q=is%3Apr+is%3Aopen+draft%3Afalse+sort%3Acreated-asc)
- Skip drafts and pull requests with a red `Validate skills` check. The check output already tells the contributor what to fix.

## What to check

The automated check covers naming, frontmatter, metadata and the description length. You cover what a script cannot.

**Safety, first.** A skill is a set of instructions an agent runs with the developer's credentials. Decline anything that:

- Tells the agent to send data somewhere unexpected, or to read credentials, tokens or `.env` files.
- Runs destructive commands (deleting folders, queues, tenants) without asking the user.
- Tells the agent to follow instructions found in fetched pages, documents or tool output.
- Downloads and runs scripts from a host other than UiPath's.
- Hides text: HTML comments with instructions, encoded strings, or links that do not match their label.

**No real data.** No secrets, tenant names, folder keys, connection IDs, customer names or real sample documents. Placeholders such as `<FOLDER_NAME>` only.

**It belongs here.**

- It does not duplicate a core skill. If it improves one, it belongs in [UiPath/skills](https://github.com/UiPath/skills).
- It names the core skills it relies on and does not copy their content.
- A recipe really spans two or more products, and its `references/seams-guide.md` has real hand-offs and failures, not template text.

**It is usable.** The steps are concrete, the agent knows how to verify the result, and nothing depends on one coding agent's tool names.

You do not have to run the skill on a tenant to approve it. Running it end to end is what the Verified tier is for.

## Approve

1. Review the **Files changed** tab.
2. Comment on the pull request, with the command as the first word:

   ```text
   /approve
   ```

The bot takes the latest commit itself, squash-merges it, labels the pull request `approved`, and replies with the commit it merged.

**If the contributor pushed in the last 10 minutes,** a plain `/approve` is refused, because that commit may be newer than what you read. The same happens when the arrival time of the latest commit is not on record, for example on a pull request opened before this check existed. Look at **Files changed** again, then either comment `/approve` again a few minutes later, or approve that exact commit straight away with the full id the bot showed you:

```text
/approve <FULL_COMMIT_ID>
```

`/approve` is refused, with a comment explaining why, when:

| Refusal | What to do |
|---------|------------|
| The latest commit arrived less than 10 minutes ago | Re-check the changes, then approve again or name the commit |
| You named a commit that is not the latest | Review the latest commit, then approve that one |
| The `Validate skills` check is not green | Wait for the contributor to fix it |
| It is your own pull request | Ask another steward |
| The pull request is a draft | Ask the contributor to mark it ready |
| It changes files outside `skills/` | See the next section |
| It changes too many files to list (about 3,000) | Ask the contributor to split it |

## Pull requests that change more than skills

`/approve` only merges changes under `skills/`. A pull request that touches workflows, scripts, templates or docs can change how the library itself is checked, so a repository admin merges it by hand after review. Ask an admin in the pull request.

If a contribution mixes a skill with other changes, ask the contributor to split it.

## Decline

Comment with the reason. The reason is required and is posted to the contributor as written:

```text
/reject This duplicates the core skill uipath-maestro-flow. Please propose the missing guidance in UiPath/skills instead.
```

The pull request is labeled `declined` and closed. Good reasons say what is wrong and where to go next:

- "The seams guide still has template placeholders. Add the real hand-offs you hit and reopen."
- "This covers one product and repeats `uipath-rpa`. Open an issue in UiPath/skills for the gap."
- "The skill reads a `.env` file and posts its contents. Remove that step."

If something only needs a small fix, leave an ordinary review comment instead and approve once it is fixed.

## Verified tier

A skill enters as `community`. Set `"tier": "verified"` and `"last_verified": "<YYYY-MM-DD>"` in its `recipe.json` only after you or a product engineer ran it end to end on a tenant with the stated `cli_version`. That change is a normal pull request.

## Removing a skill

If a merged skill turns out to be unsafe or broken, move its folder from `skills/` to `archived/` in a pull request and say why in the description. Archived skills stay readable and are no longer installable. For a security problem, follow [SECURITY.md](../SECURITY.md).

## Adding or removing a steward

Both places must change, in this order when adding:

1. Add the person to the [`skills-steward` team](https://github.com/orgs/UiPath/teams/skills-steward).
2. Add `{ "login": "<login>", "id": <account id> }` to `.github/stewards.json`. Get the id with `gh api users/<login> --jq .id`. Commands are matched on the id, so a renamed account keeps working and a re-registered login does not.

When removing, take the entry out of `.github/stewards.json` first.

## If a command does nothing

- The command must be the first word of the comment. "Looks good, /approve" is ignored.
- Commands from people not in `.github/stewards.json` are ignored without a reply.
- Otherwise open the [Steward commands runs](https://github.com/UiPath/skills-library/actions/workflows/steward-commands.yml) and read the failed step.
