import assert from "node:assert/strict";
import { cpSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { MANIFESTS, syncVersions } from "./sync-version.mjs";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

// Copies package.json and every manifest into a temp root, then runs `body` against it.
function withRepo(body) {
  const root = mkdtempSync(join(tmpdir(), "skills-library-version-"));
  try {
    for (const file of ["package.json", ...Object.keys(MANIFESTS)]) {
      mkdirSync(dirname(join(root, file)), { recursive: true });
      cpSync(join(ROOT, file), join(root, file));
    }
    return body(root);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

const readJson = (root, file) => JSON.parse(readFileSync(join(root, file), "utf8"));
const editJson = (root, file, edit) => {
  const doc = readJson(root, file);
  edit(doc);
  writeFileSync(join(root, file), JSON.stringify(doc));
};

test("the committed manifests are in sync with package.json", () => {
  const { drift, errors } = syncVersions(ROOT);
  assert.deepEqual(errors, []);
  assert.deepEqual(drift, []);
});

test("a package.json bump reaches every manifest, without the pre-release stamp", () => {
  withRepo((root) => {
    editJson(root, "package.json", (pkg) => (pkg.version = "9.9.0-preview.12"));
    const { drift, errors } = syncVersions(root, { write: true });
    assert.deepEqual(errors, []);
    assert.equal(drift.length, Object.keys(MANIFESTS).length);
    assert.equal(readJson(root, ".claude-plugin/plugin.json").version, "9.9.0");
    assert.equal(readJson(root, ".agents/plugins/marketplace.json").plugins[0].version, "9.9.0");
    assert.equal(readJson(root, "gemini-extension.json").version, "9.9.0");
    assert.deepEqual(syncVersions(root).drift, []);
  });
});

test("check mode reports drift and writes nothing", () => {
  withRepo((root) => {
    editJson(root, ".cursor-plugin/plugin.json", (doc) => (doc.version = "0.0.1"));
    const { drift } = syncVersions(root);
    assert.equal(drift.length, 1);
    assert.ok(drift[0].startsWith(".cursor-plugin/plugin.json"));
    assert.equal(readJson(root, ".cursor-plugin/plugin.json").version, "0.0.1");
  });
});

test("a manifest ahead of package.json is never downgraded", () => {
  withRepo((root) => {
    editJson(root, ".codex-plugin/plugin.json", (doc) => (doc.version = "99.0.0"));
    const { errors } = syncVersions(root, { write: true });
    assert.ok(errors.some((e) => e.includes("refusing to downgrade")));
    assert.equal(readJson(root, ".codex-plugin/plugin.json").version, "99.0.0");
  });
});

test("a marketplace with no plugin entry is an error", () => {
  withRepo((root) => {
    editJson(root, ".claude-plugin/marketplace.json", (doc) => (doc.plugins = []));
    const { errors } = syncVersions(root);
    assert.ok(errors.some((e) => e.includes("no plugins[] entry")));
  });
});
