# Releasing the package

The library is published to npm as **`@uipath/skills-library`**. The setup follows [UiPath/skills](https://github.com/UiPath/skills) (`@uipath/skills`), without that repo's CLI lockstep, flavor packages and release gate.

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

## Cut a release

1. On `main`, bump `package.json` `version`, run `npm run version:sync`, and merge the pull request.
2. Create `release/v<M.N>` from that commit and push it. That publishes a `preview`.
3. Install the preview and check it: `npm install @uipath/skills-library@preview`.
4. Run **Publish package** from the `release/v<M.N>` branch with channel `latest`.

A fix for a released line goes to `main` first, then is cherry-picked to `release/v<M.N>` with a patch bump.

## One-time setup

Done once by an `@uipath` npm organization owner before the first publish.

1. **Create the package.** npm only lets you add a trusted publisher to a package that exists, so the first version goes up by hand:
   ```bash
   npm login
   npm publish --access public --tag latest
   ```
2. **Add the trusted publisher.** On npmjs.com, open `@uipath/skills-library` → **Settings** → **Trusted publishing** → **GitHub Actions**, and enter organization `UiPath`, repository `skills-library`, workflow `publish.yml`. Leave the environment empty.
3. **Lock token publishing.** In the same settings, set **Publishing access** to require two-factor authentication and disallow tokens, so only the workflow can publish.
4. **GitHub Packages.** No setup: the `dev` job publishes with the workflow's `GITHUB_TOKEN`. The package appears under the UiPath organization after the first push to `main`.

The repository holds no npm token. Provenance needs the repository to be public and `package.json` `repository.url` to match it.
