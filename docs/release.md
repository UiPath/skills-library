# Releasing the package

The library is published to npm as **`@uipath/skills-library`**. It follows the release cycle of [UiPath/skills](https://github.com/UiPath/skills) (`@uipath/skills`): the same version line, the same biweekly cut, and stable promoted at the same time. It leaves out that repo's flavor packages, release gate and Slack announcements.

## Version model

`package.json` `version` is the only version you edit. `scripts/sync-version.mjs` copies it to every plugin manifest:

| File | Field |
|------|-------|
| `.claude-plugin/plugin.json` | `version` |
| `.claude-plugin/marketplace.json` | `plugins[0].version` |
| `.codex-plugin/plugin.json` | `version` |
| `.agents/plugins/marketplace.json` | `plugins[0].version` |
| `.cursor-plugin/plugin.json` | `version` |
| `gemini-extension.json` | `version` |

The manifests carry the plain `M.N.P` version: pre-release stamps are stripped, because plugin auto-update wants plain versions. The script refuses to lower a manifest's version, since plugin auto-update never goes backwards.

```bash
npm run version:sync    # rewrite the manifests from package.json
npm run version:check   # exit 1 if any manifest drifted
node scripts/sync-version.mjs --list-paths   # the files the script owns
```

`version:check` runs in the `Validate skills` check on every pull request, and again in the publish guard.

## Channels

Registry follows channel. `.github/workflows/publish.yml` does the publishing.

| Channel | Registry | Version | Trigger |
|---------|----------|---------|---------|
| `latest` | npmjs | `M.N.P` from `package.json` | Manual: run **Publish package** with channel `latest` |
| `preview` | npmjs | `M.N.P-preview.<run>` | Every push to `release/v*`, or a manual run with channel `preview` |
| `dev` | GitHub Packages | `M.N.P-dev.<run>` | Every push to `main`, or a manual run with channel `dev` |

Stamped versions are never committed. `npm install @uipath/skills-library` with no tag always resolves the last `latest`. Only the highest release line owns the bare `preview` tag; a push to an older `release/v*` line publishes to `preview-v<M.N>`.

Every publish first runs the guard: tests, `version:check` and `npm run validate`. The npmjs job then packs the tarball, checks it contains the plugin manifests and `skills/` but nothing from `archived/`, `templates/`, `scripts/` or `.github/`, and publishes that exact file with `--provenance`.

## Release cycle

The version line is the one `UiPath/skills` is on: `release/v1.208` here ships in the same sprint as `release/v1.208` there. `main` carries the line in development, `M.N.0`.

### Sprint cut (`sprint-release-cut.yml`)

The cut runs **Sunday 06:30 UTC**, gated to the **14-day cadence** anchored at `2026-06-14`: the same cadence and anchor as the core repo, 30 minutes after its cut. On a release Sunday it:

1. **Checks lockstep.** The core repo's `main` must still be on `M.N`, or it must already have `release/v<M.N>`. Otherwise the cut stops with an error: one repo has advanced and the other has not.
2. **Cuts `release/v<M.N>`** from `main` at the version already there. No bump.
3. **Publishes `dev` and `preview`** by dispatching `publish.yml` on the release branch, and waits for both. The dispatches are explicit, because a push made with `GITHUB_TOKEN` does not trigger other workflows.
4. **Opens a PR bumping `main` to `M.(N+1).0`**, even when the publish failed, with a warning in the body. Merge it before the next cut, or that cut stops at the lockstep check. GitHub does not start required checks on a PR opened with `GITHUB_TOKEN`: close and reopen it to run them.

The cut never publishes `latest`. If a run fails after cutting the branch, the next run finds `release/v<M.N>` at `M.N.0` and resumes.

Off-cadence or out-of-sequence cut: run **Sprint Release Cut** with `minor_override` (for example `1.208`), which skips the cadence gate and the lockstep check. `dry_run` prints the plan without pushing, publishing or opening a PR.

### Promote a line to stable

Promote the same line the core repo promotes, at the same time. Check its `latest` with `npm view @uipath/skills dist-tags`, then:

```bash
gh workflow run publish.yml --ref release/v<M.N> -f channel=latest
```

This publishes the committed `package.json` version of the release branch to `latest`. `latest` publishes only from a `release/v*` branch.

### Hotfixes

A fix goes to `main` first, then is cherry-picked to `release/v<M.N>` in a pull request, with a patch bump (`M.N.1`) and `npm run version:sync`. The merge publishes a new `preview`; promote it as above.

## One-time setup

Done once before the first publish: steps 1–4 by a GitHub admin of the `UiPath` organization, steps 5–8 by an owner of the `@uipath` npm organization.

### GitHub

1. **Make the repository public.** Provenance needs it, and `package.json` `repository.url` must match it.
2. **Let the workflows do their jobs.** In **Settings** → **Actions** → **General**, keep **Workflow permissions** read-only, and turn on **Allow GitHub Actions to create and approve pull requests**: the sprint cut opens the version-bump PR with `GITHUB_TOKEN`.
3. **Protect the release lines.** In **Settings** → **Rules** → **Rulesets**, add a ruleset for `release/v*` that restricts creations and updates, blocks force pushes and deletion, and requires a pull request and the `Validate skills` check. Give bypass only to the release-owner team and to GitHub Actions, which creates the branch during the cut. Protect `main` the same way, without the Actions bypass: the cut never pushes to `main`.
4. **Create the `npm-publish` environment.** In **Settings** → **Environments** → **New environment**, name it `npm-publish`, and under **Deployment branches and tags** choose **Selected branches and tags** with the rules `main` and `release/v*`. Required reviewers are optional: with them, every npmjs publish waits for an approval, including the preview each sprint cut publishes.

### npmjs

5. **Create the package.** npm only lets you add a trusted publisher to a package that exists, so the first version goes up by hand. Publish it as a preview from `main`, so no stable version gets ahead of the core repo:
   ```bash
   npm login
   npm version "$(node -p "require('./package.json').version")-preview.0" --no-git-tag-version
   npm publish --access public --tag preview
   git checkout package.json
   ```
   A brand-new package has no other version, so npm may also point `latest` at this one. The first stable promotion replaces it.
6. **Add the trusted publisher.** Open `@uipath/skills-library` → **Settings** → **Trusted publishing** → **GitHub Actions**, and enter organization `UiPath`, repository `skills-library`, workflow `publish.yml`, environment `npm-publish`. The environment is what stops a run from any other branch from publishing: a dispatch runs the workflow file from the chosen branch, so a check inside the file alone can be edited away.
7. **Lock token publishing.** In the same settings, set **Publishing access** to require two-factor authentication and disallow tokens, so only the workflow can publish.
8. **Give the release-owner team access.** In the `@uipath` organization's **Teams**, give that team read-write access to the package, so publishing never depends on one person.

GitHub Packages needs no setup: the `dev` job publishes with `GITHUB_TOKEN`, and the package appears under the UiPath organization after the first push to `main`. The repository holds no npm token.
