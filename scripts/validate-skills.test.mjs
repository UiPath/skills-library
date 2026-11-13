import assert from "node:assert/strict";
import { cpSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { validateAll } from "./validate-skills.mjs";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const TEMPLATE = join(ROOT, "templates", "skill");

// Copies the template into a temp skills dir under `folder`, renamed to match, then applies `edit`.
function errorsFor(folder, edit = () => {}) {
  const skillsDir = mkdtempSync(join(tmpdir(), "skills-library-"));
  try {
    const dir = join(skillsDir, folder);
    cpSync(TEMPLATE, dir, { recursive: true });
    const skillPath = join(dir, "SKILL.md");
    const metaPath = join(dir, "recipe.json");
    writeFileSync(skillPath, readFileSync(skillPath, "utf8").replace("name: your-skill-name", `name: ${folder}`));
    const meta = JSON.parse(readFileSync(metaPath, "utf8"));
    meta.name = folder;
    edit({ dir, meta, skillPath });
    writeFileSync(metaPath, JSON.stringify(meta));
    return validateAll(skillsDir)[0].errors;
  } finally {
    rmSync(skillsDir, { recursive: true, force: true });
  }
}

test("the template passes once renamed", () => {
  assert.deepEqual(errorsFor("invoice-intake-ixp-maestro"), []);
});

test("names reserved for or used by the core repo are rejected", () => {
  const errors = errorsFor("uipath-rpa");
  assert.ok(errors.some((e) => e.includes("reserved for the core repo")));
  assert.ok(errors.some((e) => e.includes("already used by a core skill")));
});

test("a recipe needs two products and a seams guide", () => {
  const oneProduct = errorsFor("single-product", ({ meta }) => { meta.products = ["Maestro Flow"]; });
  assert.ok(oneProduct.some((e) => e.includes("at least 2 different products")));

  const noSeams = errorsFor("no-seams", ({ dir }) => rmSync(join(dir, "references"), { recursive: true }));
  assert.ok(noSeams.some((e) => e.includes("seams-guide.md")));
});

test("kind skill accepts one product and no seams guide", () => {
  const errors = errorsFor("activity-helper", ({ dir, meta }) => {
    meta.kind = "skill";
    meta.products = ["Studio"];
    rmSync(join(dir, "references"), { recursive: true });
  });
  assert.deepEqual(errors, []);
});

test("frontmatter and metadata problems are reported", () => {
  const errors = errorsFor("bad-meta", ({ meta, skillPath }) => {
    meta.tier = "official";
    meta.extra = true;
    delete meta.cli_version;
    const text = readFileSync(skillPath, "utf8");
    writeFileSync(skillPath, text.replace("UiPath-approved community skill. Maintained by its author.", "").replace(/description: ".*"/, `description: "${"x".repeat(1025)}"`));
  });
  for (const expected of ["tier must be one of", 'unknown field "extra"', 'missing required field "cli_version"', "the limit is 1024", "body must contain the line"]) {
    assert.ok(errors.some((e) => e.includes(expected)), `expected an error containing: ${expected}`);
  }
});

test("symbolic links inside a skill folder are rejected", () => {
  const errors = errorsFor("linked-skill", ({ dir }) => symlinkSync("/etc/hosts", join(dir, "references", "hosts.md")));
  assert.equal(errors.length, 1);
  assert.ok(errors[0].includes("symbolic links are not allowed"));
  assert.ok(errors[0].includes("references/hosts.md"));
});

test("a verified skill needs last_verified", () => {
  const errors = errorsFor("verified-one", ({ meta }) => { meta.tier = "verified"; });
  assert.ok(errors.some((e) => e.includes("needs last_verified")));
});

test("every plugin manifest carries the package.json version and the plugin name", () => {
  const read = (path) => JSON.parse(readFileSync(join(ROOT, path), "utf8"));
  const version = read("package.json").version;
  const manifests = [
    read(".claude-plugin/plugin.json"),
    read(".claude-plugin/marketplace.json").plugins[0],
    read(".cursor-plugin/plugin.json"),
    read(".codex-plugin/plugin.json"),
    read(".agents/plugins/marketplace.json").plugins[0],
    read("gemini-extension.json"),
  ];
  for (const manifest of manifests) {
    assert.equal(manifest.version, version);
    assert.equal(manifest.name, "uipath-library");
  }
});
