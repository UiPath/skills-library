import assert from "node:assert/strict";
import { test } from "node:test";
import { decide, effectiveArrival, filesOutsideContributions, headArrival, isSteward, parseCommand } from "./steward-command.mjs";

const STEWARD = 101;
const OTHER_STEWARD = 102;
const CONTRIBUTOR = 900;
const HEAD = "abc1234def5678900000000000000000000000ff";
const stewards = [{ login: "steward-one", id: STEWARD }, { login: "steward-two", id: OTHER_STEWARD }];
const openPr = { state: "open", draft: false, authorId: CONTRIBUTOR, headSha: HEAD, changedFiles: 2 };
const skillPaths = ["skills/invoice-intake/SKILL.md", "skills/invoice-intake/recipe.json"];
const COMMENTED_AT = Date.parse("2026-01-01T12:00:00Z");
const minutesBefore = (minutes) => COMMENTED_AT - minutes * 60_000;
const run = (body, overrides = {}) =>
  decide({
    parsed: parseCommand(body), commenterId: STEWARD, stewards, pr: openPr, changedPaths: skillPaths,
    listedFiles: skillPaths.length, checkPassed: true, headArrivedAt: minutesBefore(60), commentedAt: COMMENTED_AT, ...overrides,
  });

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
  assert.equal(run(`/approve ${HEAD}`, { commenterId: CONTRIBUTOR }).action, "ignore");
  assert.equal(run("/reject spam", { commenterId: CONTRIBUTOR }).action, "ignore");
});

test("approve merges an open, ready, green skills pull request from someone else", () => {
  assert.equal(run(`/approve ${HEAD}`).action, "approve");
  assert.equal(run(`/approve ${HEAD.toUpperCase()}`).action, "approve");
  assert.equal(run(`/approve ${HEAD}`, { checkPassed: false }).action, "comment");
  assert.equal(run(`/approve ${HEAD}`, { pr: { ...openPr, draft: true } }).action, "comment");
  assert.equal(run(`/approve ${HEAD}`, { pr: { ...openPr, state: "closed" } }).action, "comment");
  assert.equal(run(`/approve ${HEAD}`, { pr: { ...openPr, authorId: STEWARD } }).action, "comment");
});

test("a plain approve uses the latest commit once it has settled", () => {
  assert.equal(run("/approve").action, "approve");
  assert.equal(run("/approve", { headArrivedAt: minutesBefore(10) }).action, "approve");
  const fresh = run("/approve", { headArrivedAt: minutesBefore(3) });
  assert.equal(fresh.action, "comment");
  assert.ok(fresh.message.includes(`/approve ${HEAD}`));
  assert.equal(run("/approve", { headArrivedAt: minutesBefore(-1) }).action, "comment"); // pushed after the comment
  assert.equal(run("/approve", { headArrivedAt: null }).action, "comment"); // unknown arrival time fails closed
});

test("head arrival comes from this pull request's own record, newest first", () => {
  const bot = { login: "github-actions[bot]" };
  const status = (context, created_at, creator = bot) => ({ context, created_at, creator });
  const statuses = [
    status("Head arrival (PR #7)", "2026-01-01T11:00:00Z"),
    status("Head arrival (PR #7)", "2026-01-01T09:00:00Z"),
    status("Head arrival (PR #8)", "2026-01-01T11:30:00Z"), // same commit in another pull request
    status("Head arrival (PR #7)", "2026-01-01T11:45:00Z", { login: "someone" }), // not written by the workflow
    status("Validate skills", "2026-01-01T11:50:00Z"),
  ];
  assert.equal(headArrival(statuses, "7"), Date.parse("2026-01-01T11:00:00Z"));
  assert.equal(headArrival(statuses, "9"), null);
  assert.equal(headArrival([], "7"), null);
});

test("a force-push after the record makes the commit count as newly arrived", () => {
  const recorded = Date.parse("2026-01-01T09:00:00Z");
  const forcePush = (created_at) => ({ event: "head_ref_force_pushed", created_at });
  const timeline = [{ event: "commented", created_at: "2026-01-01T11:59:00Z" }, forcePush("2026-01-01T10:00:00Z"), forcePush("2026-01-01T11:58:00Z")];
  assert.equal(effectiveArrival(recorded, timeline), Date.parse("2026-01-01T11:58:00Z"));
  assert.equal(effectiveArrival(recorded, [forcePush("2026-01-01T08:00:00Z")]), recorded);
  assert.equal(effectiveArrival(recorded, []), recorded);
  for (const event of ["reopened", "head_ref_restored", "head_ref_deleted"]) {
    assert.equal(effectiveArrival(recorded, [{ event, created_at: "2026-01-01T11:00:00Z" }]), Date.parse("2026-01-01T11:00:00Z"));
  }
  assert.equal(effectiveArrival(null, timeline), null); // no record: fail closed
  assert.equal(effectiveArrival(recorded, null), null); // timeline too long to read: fail closed
});

test("naming the commit approves it at once, and only if it is the latest", () => {
  assert.equal(run(`/approve ${HEAD}`, { headArrivedAt: minutesBefore(1) }).action, "approve");
  assert.equal(run("/approve abc1234").action, "comment"); // a prefix could match a second, crafted commit
  const stale = run(`/approve ${"1".repeat(40)}`);
  assert.equal(stale.action, "comment");
  assert.ok(stale.message.includes("not the latest commit"));
});

test("approve refuses a pull request that changes files outside skills/", () => {
  assert.deepEqual(filesOutsideContributions([...skillPaths, ".github/workflows/validate.yml", "skillsX/a.md"]), [
    ".github/workflows/validate.yml",
    "skillsX/a.md",
  ]);
  const decision = run(`/approve ${HEAD}`, { changedPaths: [...skillPaths, "scripts/validate-skills.mjs"] });
  assert.equal(decision.action, "comment");
  assert.ok(decision.message.includes("scripts/validate-skills.mjs"));
});

test("approve refuses when the file listing may be incomplete", () => {
  assert.equal(run(`/approve ${HEAD}`, { listedFiles: 1 }).action, "comment");
  const huge = run(`/approve ${HEAD}`, { pr: { ...openPr, changedFiles: 3000 }, listedFiles: 3000 });
  assert.equal(huge.action, "comment");
  assert.ok(huge.message.includes("too many files"));
});

test("reject needs a reason", () => {
  assert.equal(run("/reject").action, "comment");
  assert.equal(run("/reject belongs in the core repo").action, "reject");
  assert.equal(run("/reject not ready", { checkPassed: false }).action, "reject");
});
