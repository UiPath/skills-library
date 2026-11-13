import assert from "node:assert/strict";
import { test } from "node:test";
import { decide, filesOutsideContributions, isSteward, parseCommand } from "./steward-command.mjs";

const STEWARD = 101;
const OTHER_STEWARD = 102;
const CONTRIBUTOR = 900;
const HEAD = "abc1234def5678900000000000000000000000ff";
const stewards = [{ login: "steward-one", id: STEWARD }, { login: "steward-two", id: OTHER_STEWARD }];
const openPr = { state: "open", draft: false, authorId: CONTRIBUTOR, headSha: HEAD };
const skillPaths = ["skills/invoice-intake/SKILL.md", "skills/invoice-intake/recipe.json"];
const run = (body, overrides = {}) =>
  decide({ parsed: parseCommand(body), commenterId: STEWARD, stewards, pr: openPr, changedPaths: skillPaths, checkPassed: true, ...overrides });

test("parseCommand reads the command and its argument", () => {
  assert.deepEqual(parseCommand("/approve abc1234"), { command: "approve", argument: "abc1234" });
  assert.deepEqual(parseCommand("  /reject duplicates a core skill\nsee UiPath/skills "), {
    command: "reject",
    argument: "duplicates a core skill\nsee UiPath/skills",
  });
  assert.equal(parseCommand("please /approve this"), null);
  assert.equal(parseCommand("/approved"), null);
  assert.equal(parseCommand(""), null);
});

test("stewards are matched on account id, not login", () => {
  assert.ok(isSteward(STEWARD, stewards));
  assert.ok(!isSteward(CONTRIBUTOR, stewards));
  assert.ok(!isSteward(Number("not-a-number"), stewards));
});

test("a non-steward's command is ignored", () => {
  assert.equal(run("/approve abc1234", { commenterId: CONTRIBUTOR }).action, "ignore");
  assert.equal(run("/reject spam", { commenterId: CONTRIBUTOR }).action, "ignore");
});

test("approve merges an open, ready, green skills pull request from someone else", () => {
  assert.equal(run("/approve abc1234").action, "approve");
  assert.equal(run(`/approve ${HEAD}`).action, "approve");
  assert.equal(run("/approve abc1234", { checkPassed: false }).action, "comment");
  assert.equal(run("/approve abc1234", { pr: { ...openPr, draft: true } }).action, "comment");
  assert.equal(run("/approve abc1234", { pr: { ...openPr, state: "closed" } }).action, "comment");
  assert.equal(run("/approve abc1234", { pr: { ...openPr, authorId: STEWARD } }).action, "comment");
});

test("approve is bound to the commit the steward names", () => {
  const missing = run("/approve");
  assert.equal(missing.action, "comment");
  assert.ok(missing.message.includes("/approve abc1234"));
  assert.equal(run("/approve abc12").action, "comment"); // too short to identify a commit
  const stale = run("/approve 1111111");
  assert.equal(stale.action, "comment");
  assert.ok(stale.message.includes("not the latest commit"));
});

test("approve refuses a pull request that changes files outside skills/", () => {
  assert.deepEqual(filesOutsideContributions([...skillPaths, ".github/workflows/validate.yml", "skillsX/a.md"]), [
    ".github/workflows/validate.yml",
    "skillsX/a.md",
  ]);
  const decision = run("/approve abc1234", { changedPaths: [...skillPaths, "scripts/validate-skills.mjs"] });
  assert.equal(decision.action, "comment");
  assert.ok(decision.message.includes("scripts/validate-skills.mjs"));
});

test("reject needs a reason", () => {
  assert.equal(run("/reject").action, "comment");
  assert.equal(run("/reject belongs in the core repo").action, "reject");
  assert.equal(run("/reject not ready", { checkPassed: false }).action, "reject");
});
