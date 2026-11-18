#!/usr/bin/env node
// Propagates the package.json version to every plugin manifest, so npm and the
// plugin marketplaces always ship the same version. package.json is the only
// version you edit. Pre-release suffixes (the dev/preview stamps publish.yml adds)
// are stripped: plugin auto-update wants plain M.N.P versions.
// No dependencies. Usage:
//   node scripts/sync-version.mjs               # rewrite the manifests
//   node scripts/sync-version.mjs --check       # exit 1 if any manifest drifted
//   node scripts/sync-version.mjs --list-paths  # print the files this script owns

import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

// Repo-relative path -> where the version lives in that file.
export const MANIFESTS = {
  ".claude-plugin/plugin.json": "top",
  ".claude-plugin/marketplace.json": "plugins[0]",
  ".codex-plugin/plugin.json": "top",
  ".agents/plugins/marketplace.json": "plugins[0]",
  ".cursor-plugin/plugin.json": "top",
  "gemini-extension.json": "top",
};

const readJson = (path) => JSON.parse(readFileSync(path, "utf8"));
const writeJson = (path, doc) => writeFileSync(path, `${JSON.stringify(doc, null, 2)}\n`);

// `1.2.0-dev.42` -> `1.2.0`
export const baseVersion = (version) => version.split("-")[0];

function compareBase(a, b) {
  const pa = baseVersion(a).split(".").map(Number);
  const pb = baseVersion(b).split(".").map(Number);
  for (let i = 0; i < 3; i++) if (pa[i] !== pb[i]) return pa[i] < pb[i] ? -1 : 1;
  return 0;
}

// Returns { version, drift: ["file: old -> new"], errors: [...] }. Writes only when
// `write` is true and there are no errors.
export function syncVersions(root, { write = false } = {}) {
  const version = baseVersion(readJson(join(root, "package.json")).version);
  const drift = [];
  const errors = [];
  const writes = [];

  for (const [file, location] of Object.entries(MANIFESTS)) {
    const path = join(root, file);
    const doc = readJson(path);
    let holder = doc;
    if (location === "plugins[0]") {
      // A marketplace with no plugin entry has nothing to sync; treating that as a
      // no-op is how a manifest silently stops tracking releases.
      if (!Array.isArray(doc.plugins) || doc.plugins.length === 0) {
        errors.push(`${file}: no plugins[] entry to carry the version`);
        continue;
      }
      holder = doc.plugins[0];
    }
    if (holder.version === version) continue;
    // Plugin auto-update never goes backwards, so a lower version would freeze users.
    if (typeof holder.version === "string" && compareBase(version, holder.version) < 0) {
      errors.push(`${file}: refusing to downgrade ${holder.version} to package.json ${version}`);
      continue;
    }
    drift.push(`${file}: ${holder.version} -> ${version}`);
    holder.version = version;
    writes.push(() => writeJson(path, doc));
  }

  if (write && errors.length === 0) for (const w of writes) w();
  return { version, drift, errors };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  if (process.argv.includes("--list-paths")) {
    // Stdout is the paths alone: release automation reads it to know what to stage.
    for (const file of ["package.json", ...Object.keys(MANIFESTS)]) console.log(file);
    process.exit(0);
  }
  const check = process.argv.includes("--check");
  const { version, drift, errors } = syncVersions(ROOT, { write: !check });
  for (const error of errors) console.error(`✗ ${error}`);
  if (errors.length) process.exit(1);
  if (check && drift.length) {
    console.error("✗ Version drift (run `npm run version:sync`):");
    for (const d of drift) console.error(`  - ${d}`);
    process.exit(1);
  }
  if (drift.length && !check) {
    console.log(`✓ Synced ${drift.length} manifest(s) to ${version}:`);
    for (const d of drift) console.log(`  - ${d}`);
  } else {
    console.log(`✓ All manifests at ${version}.`);
  }
}
