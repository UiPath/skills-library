import assert from "node:assert/strict";
import { test } from "node:test";
import { decide, isSteward, parseCommand } from "./steward-command.mjs";

const stewards = ["Steward-One", "steward-two"];
const openPr = { state: "open", draft: false, author: "contributor" };
const run = (body, overrides = {}) =>
  decide({ parsed: parseCommand(body), commenter: "steward-one", stewards, pr: openPr, checkPassed: true, ...overrides });

test("parseCommand reads the command and the reason", () => {
  assert.deepEqual(parseCommand("/approve"), { command: "approve", reason: "" });
  assert.deepEqual(parseCommand("  /reject duplicates a core skill\nsee UiPath/skills "), {
    command: "reject",
    reason: "duplicates a core skill\nsee UiPath/skills",
  });
  assert.equal(parseCommand("please /approve this"), null);
  assert.equal(parseCommand("/approved"), null);
  assert.equal(parseCommand(""), null);
});

test("steward matching ignores case", () => {
  assert.ok(isSteward("steward-one", stewards));
  assert.ok(!isSteward("someone-else", stewards));
});

test("a non-steward's command is ignored", () => {
  assert.equal(run("/approve", { commenter: "contributor" }).action, "ignore");
  assert.equal(run("/reject spam", { commenter: "contributor" }).action, "ignore");
});

test("approve merges only an open, ready, green pull request from someone else", () => {
  assert.equal(run("/approve").action, "approve");
  assert.equal(run("/approve", { checkPassed: false }).action, "comment");
  assert.equal(run("/approve", { pr: { ...openPr, draft: true } }).action, "comment");
  assert.equal(run("/approve", { pr: { ...openPr, state: "closed" } }).action, "comment");
  assert.equal(run("/approve", { pr: { ...openPr, author: "Steward-One" } }).action, "comment");
});

test("reject needs a reason", () => {
  assert.equal(run("/reject").action, "comment");
  assert.equal(run("/reject belongs in the core repo").action, "reject");
  assert.equal(run("/reject not ready", { checkPassed: false }).action, "reject");
});
