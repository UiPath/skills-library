#!/usr/bin/env node
// Validates every skill folder under skills/ against the library contract:
// naming, SKILL.md frontmatter, recipe.json metadata, and the recipe seams guide.
// No dependencies. Usage: node scripts/validate-skills.mjs [skillsDir]

import { existsSync, lstatSync, readdirSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const NAME_PATTERN = /^[a-z0-9]+(-[a-z0-9]+)*$/;
const RESERVED_PREFIX = "uipath-";
const DESCRIPTION_LIMIT = 1024;
const DISCLAIMER = "UiPath-approved community skill. Maintained by its author.";
const MIN_RECIPE_PRODUCTS = 2;
const KEEP_FILE = ".gitkeep";

const readJson = (path) => JSON.parse(readFileSync(path, "utf8"));

function parseFrontmatter(text) {
  const match = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(text);
  if (!match) return null;
  const fields = {};
  for (const line of match[1].split(/\r?\n/)) {
    const field = /^([A-Za-z_-]+):\s*(.*)$/.exec(line);
    if (!field) continue;
    let value = field[2].trim();
    if (/^".*"$/.test(value) || /^'.*'$/.test(value)) value = value.slice(1, -1);
    fields[field[1]] = value;
  }
  return { fields, body: text.slice(match[0].length) };
}

function checkMetadata(meta, schema, folder, dir) {
  const errors = [];
  const props = schema.properties;
  for (const key of schema.required) {
    if (meta[key] === undefined) errors.push(`recipe.json: missing required field "${key}"`);
  }
  for (const key of Object.keys(meta)) {
    if (!(key in props)) errors.push(`recipe.json: unknown field "${key}"`);
  }
  if (meta.name !== undefined && meta.name !== folder) {
    errors.push(`recipe.json: name "${meta.name}" must equal the folder name "${folder}"`);
  }
  for (const key of ["kind", "tier"]) {
    if (meta[key] !== undefined && !props[key].enum.includes(meta[key])) {
      errors.push(`recipe.json: ${key} must be one of ${props[key].enum.join(", ")}`);
    }
  }
  if (meta.authors !== undefined) {
    const valid = Array.isArray(meta.authors) && meta.authors.length > 0 &&
      meta.authors.every((a) => a && typeof a.github === "string" && a.github.trim() && !a.github.startsWith("@"));
    if (!valid) errors.push('recipe.json: authors must be a non-empty list of { "github": "<login>" } without "@"');
  }
  if (meta.products !== undefined) {
    const valid = Array.isArray(meta.products) && meta.products.length > 0 &&
      meta.products.every((p) => typeof p === "string" && p.trim());
    if (!valid) {
      errors.push("recipe.json: products must be a non-empty list of product names");
    } else if (meta.kind === "recipe" && new Set(meta.products).size < MIN_RECIPE_PRODUCTS) {
      errors.push(`recipe.json: a recipe lists at least ${MIN_RECIPE_PRODUCTS} different products; use kind "skill" for one product`);
    }
  }
  if (meta.cli_version !== undefined && (typeof meta.cli_version !== "string" || !meta.cli_version.trim())) {
    errors.push("recipe.json: cli_version must be a version string such as 1.207.0");
  }
  const datePattern = new RegExp(props.last_verified.pattern);
  if (meta.last_verified !== undefined && !datePattern.test(String(meta.last_verified))) {
    errors.push("recipe.json: last_verified must be a date in YYYY-MM-DD form");
  }
  if (meta.tier === "verified" && meta.last_verified === undefined) {
    errors.push("recipe.json: a verified skill needs last_verified");
  }
  if (meta.upstream !== undefined) {
    const sha = new RegExp(props.upstream.properties.sha.pattern);
    if (typeof meta.upstream?.repo !== "string" || !sha.test(String(meta.upstream?.sha))) {
      errors.push("recipe.json: upstream needs repo and a full 40-character commit sha");
    }
  }
  if (meta.kind === "recipe" && !existsSync(join(dir, "references", "seams-guide.md"))) {
    errors.push("a recipe needs references/seams-guide.md with the hand-off table");
  }
  return errors;
}

// A symbolic link could point outside the skill folder, so none are allowed in a contribution.
function symlinksIn(dir, prefix = "") {
  const found = [];
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    const stats = lstatSync(path);
    if (stats.isSymbolicLink()) found.push(prefix + entry);
    else if (stats.isDirectory()) found.push(...symlinksIn(path, `${prefix}${entry}/`));
  }
  return found;
}

export function validateSkill(dir, folder, { schema, coreNames }) {
  const links = symlinksIn(dir);
  if (links.length > 0) return [`symbolic links are not allowed in a skill folder: ${links.join(", ")}`];
  const errors = [];
  if (!NAME_PATTERN.test(folder)) errors.push("folder name must be kebab-case (lowercase letters, digits, hyphens)");
  if (folder.startsWith(RESERVED_PREFIX)) errors.push(`names starting with "${RESERVED_PREFIX}" are reserved for the core repo, UiPath/skills`);
  if (coreNames.includes(folder)) errors.push("name is already used by a core skill in UiPath/skills");

  const skillPath = join(dir, "SKILL.md");
  if (!existsSync(skillPath)) {
    errors.push("missing SKILL.md");
  } else {
    const parsed = parseFrontmatter(readFileSync(skillPath, "utf8"));
    if (!parsed) {
      errors.push("SKILL.md: missing YAML frontmatter (--- name / description ---)");
    } else {
      const { name, description } = parsed.fields;
      if (name !== folder) errors.push(`SKILL.md: frontmatter name "${name ?? ""}" must equal the folder name "${folder}"`);
      if (!description) errors.push("SKILL.md: frontmatter description is required");
      else if (description.length > DESCRIPTION_LIMIT) {
        errors.push(`SKILL.md: description is ${description.length} characters; the limit is ${DESCRIPTION_LIMIT}`);
      }
      if (!parsed.body.includes(DISCLAIMER)) errors.push(`SKILL.md: body must contain the line "${DISCLAIMER}"`);
    }
  }

  const metaPath = join(dir, "recipe.json");
  if (!existsSync(metaPath)) {
    errors.push("missing recipe.json");
  } else {
    let meta;
    try {
      meta = readJson(metaPath);
    } catch (error) {
      errors.push(`recipe.json: not valid JSON (${error.message})`);
    }
    if (meta) errors.push(...checkMetadata(meta, schema, folder, dir));
  }
  return errors;
}

export function validateAll(skillsDir) {
  const context = {
    schema: readJson(join(ROOT, "schemas", "recipe.schema.json")),
    coreNames: readJson(join(ROOT, "assets", "core-skill-names.json")).names,
  };
  const entries = existsSync(skillsDir) ? readdirSync(skillsDir).filter((entry) => entry !== KEEP_FILE).sort() : [];
  // Anything that is not a real directory (a file, a symbolic link) is reported, never skipped.
  return entries.map((folder) => {
    const path = join(skillsDir, folder);
    if (!lstatSync(path).isDirectory()) {
      return { folder, errors: ["only skill folders are allowed directly under skills/ (no files or symbolic links)"] };
    }
    return { folder, errors: validateSkill(path, folder, context) };
  });
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const skillsDir = resolve(process.argv[2] ?? join(ROOT, "skills"));
  const results = validateAll(skillsDir);
  let failed = 0;
  for (const { folder, errors } of results) {
    if (errors.length === 0) {
      console.log(`ok    ${folder}`);
      continue;
    }
    failed += 1;
    console.log(`FAIL  ${folder}`);
    for (const error of errors) console.log(`      - ${error}`);
  }
  console.log(`\n${results.length} skill(s) checked, ${failed} failed.`);
  process.exit(failed ? 1 : 0);
}
