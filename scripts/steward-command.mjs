#!/usr/bin/env node
// Handles the steward commands typed as a pull request comment:
//   /approve            merge the pull request (squash) at its latest commit
//   /approve <sha>      the same, but only if <sha> (full 40-character id) is the latest commit
//   /reject <reason>    post the reason, label and close the pull request
// Run by .github/workflows/steward-commands.yml from the default branch.

import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const REQUIRED_CHECK = "Validate skills";
const CHECK_APP = "github-actions";
const ACTIONS_BOT = "github-actions[bot]";
const ARRIVAL_STATUS_PREFIX = "Head arrival (PR #";
const STATUSES_PER_PAGE = 100;
// Required status on main. Only this workflow sets it, so a pull request cannot merge without /approve.
const APPROVAL_STATUS = "Steward approval";
// /approve only merges contributions. Anything else (workflows, scripts, docs) is merged by a maintainer.
const CONTRIBUTION_ROOT = "skills/";
// The full commit id: a short prefix can be matched by a second, crafted commit.
const SHA_PATTERN = /^[0-9a-f]{40}$/;
// A plain /approve is refused when the latest commit arrived this recently before the comment:
// the steward may have reviewed the commit before it. Naming the commit skips the wait.
const SETTLE_MINUTES = 10;
const MS_PER_MINUTE = 60_000;
// GitHub lists at most this many files for a pull request; a longer listing is cut short.
const MAX_LISTED_FILES = 3000;
const MERGE_ATTEMPTS = 4;
const FILES_PER_PAGE = 100;
const LABELS = {
  approved: { color: "1f8a5b", description: "Approved by a UiPath steward" },
  declined: { color: "b7791f", description: "Declined by a UiPath steward, with a reason" },
};
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

// The command must be the first word of the comment. Everything after it is the argument:
// the reviewed commit for /approve, the reason for /reject.
export function parseCommand(body) {
  const match = /^\/(approve|reject)(?:[ \t]+([\s\S]*))?$/.exec(String(body ?? "").trim());
  if (!match) return null;
  return { command: match[1], argument: (match[2] ?? "").trim() };
}

// Stewards are matched on the numeric account id: a login can be renamed and re-registered.
export function isSteward(id, stewards) {
  return Number.isInteger(id) && stewards.some((steward) => steward.id === id);
}

export function filesOutsideContributions(paths) {
  return paths.filter((path) => !path.startsWith(CONTRIBUTION_ROOT));
}

// Decides what to do. Pure, so it can be tested without GitHub.
// pr: { state, draft, authorId, headSha, changedFiles }; changedPaths, listedFiles and checkPassed are only
// read for /approve. listedFiles is how many files the listing returned; changedFiles is GitHub's own count.
// headArrivedAt is when the latest commit became this pull request's head, as recorded by the
// "Record head commit" workflow (null when there is no record); commentedAt is when the steward
// commented. Both are epoch milliseconds.
export function decide({ parsed, commenterId, stewards, pr, changedPaths = [], listedFiles = 0, checkPassed = false, headArrivedAt = null, commentedAt = null }) {
  if (!parsed) return { action: "ignore", why: "no steward command" };
  if (!isSteward(commenterId, stewards)) return { action: "ignore", why: "the commenter is not a steward" };
  if (pr.state !== "open") return { action: "comment", message: "This pull request is not open, so there is nothing to approve or decline." };

  if (parsed.command === "reject") {
    if (!parsed.argument) {
      return { action: "comment", message: "A reason is required. Use `/reject <reason>` so the contributor knows what to change or where to go instead." };
    }
    return { action: "reject" };
  }

  const latest = pr.headSha;
  if (pr.authorId === commenterId) {
    return { action: "comment", message: "Stewards cannot `/approve` their own pull request. Ask another steward." };
  }
  if (pr.draft) return { action: "comment", message: "This pull request is still a draft. Mark it ready for review first." };
  const sha = parsed.argument.toLowerCase();
  const namedCommit = sha !== "";
  if (namedCommit && !SHA_PATTERN.test(sha)) {
    return { action: "comment", message: `Use \`/approve\` on its own, or name the commit with its full 40-character id. The latest commit is \`${latest}\`.` };
  }
  if (namedCommit && sha !== pr.headSha) {
    return { action: "comment", message: `\`${sha}\` is not the latest commit, so new changes arrived after your review. The latest commit is \`${latest}\`: review it, then comment \`/approve ${latest}\`.` };
  }
  if (pr.changedFiles >= MAX_LISTED_FILES || listedFiles !== pr.changedFiles) {
    return { action: "comment", message: "This pull request changes too many files to list completely, so `/approve` cannot confirm they are all under `skills/`. Ask the contributor to split it, or ask a maintainer to merge it." };
  }
  const outside = filesOutsideContributions(changedPaths);
  if (outside.length > 0) {
    const shown = outside.slice(0, 5).map((path) => `\`${path}\``).join(", ");
    return { action: "comment", message: `\`/approve\` only merges changes under \`${CONTRIBUTION_ROOT}\`. This pull request also changes ${shown}${outside.length > 5 ? " and more" : ""}, so a maintainer has to merge it.` };
  }
  if (!checkPassed) {
    return { action: "comment", message: `Cannot approve yet: the \`${REQUIRED_CHECK}\` check has not passed on the latest commit.` };
  }
  if (!namedCommit) {
    const settled = Number.isFinite(headArrivedAt) && Number.isFinite(commentedAt) &&
      commentedAt - headArrivedAt >= SETTLE_MINUTES * MS_PER_MINUTE;
    if (!settled) {
      return { action: "comment", message: `The latest commit \`${latest}\` arrived less than ${SETTLE_MINUTES} minutes before your approval, or its arrival time is not on record, so it may be newer than what you reviewed. Check **Files changed** again, then either comment \`/approve\` again in a few minutes or approve this exact commit now with \`/approve ${latest}\`.` };
    }
  }
  return { action: "approve" };
}

function client(token, repository) {
  const base = `https://api.github.com/repos/${repository}`;
  return async function api(method, path, body) {
    const response = await fetch(`${base}${path}`, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        ...(body ? { "Content-Type": "application/json" } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
    const text = await response.text();
    const data = text ? JSON.parse(text) : null;
    if (!response.ok) {
      const error = new Error(`${method} ${path} failed with ${response.status}: ${data?.message ?? text}`);
      error.status = response.status;
      throw error;
    }
    return data;
  };
}

// Every path the pull request touches, including the old path of a rename, plus how many
// files the listing returned so the caller can tell a complete listing from a cut one.
async function changedPathsOf(api, number) {
  const paths = [];
  let listedFiles = 0;
  for (let page = 1; ; page += 1) {
    const files = await api("GET", `/pulls/${number}/files?per_page=${FILES_PER_PAGE}&page=${page}`);
    listedFiles += files.length;
    for (const file of files) {
      paths.push(file.filename);
      if (file.previous_filename) paths.push(file.previous_filename);
    }
    if (files.length < FILES_PER_PAGE) return { paths, listedFiles };
  }
}

// The check counts only when GitHub Actions reported it. With every changed file under skills/,
// the workflow and validator that produced it are the ones on the default branch.
async function requiredCheckPassed(api, sha) {
  const runs = await api("GET", `/commits/${sha}/check-runs?check_name=${encodeURIComponent(REQUIRED_CHECK)}&filter=latest`);
  const trusted = runs.check_runs.filter((run) => run.app?.slug === CHECK_APP);
  return trusted.length > 0 && trusted.every((run) => run.conclusion === "success");
}

// When this commit last became the head of this pull request, or null when there is no record.
// The record is a commit status written by the "Record head commit" workflow. Its context names
// the pull request, so the same commit sitting in another pull request or branch does not count,
// and the newest record wins, so a commit that is pushed away and pushed back counts as new.
export function headArrival(statuses, number) {
  const times = statuses
    .filter((status) => status.context === `${ARRIVAL_STATUS_PREFIX}${number})` && status.creator?.login === ACTIONS_BOT)
    .map((status) => Date.parse(status.created_at))
    .filter(Number.isFinite);
  return times.length > 0 ? Math.max(...times) : null;
}

async function addLabel(api, number, name) {
  try {
    await api("POST", "/labels", { name, ...LABELS[name] });
  } catch (error) {
    if (error.status !== 422) throw error; // 422: the label already exists
  }
  await api("POST", `/issues/${number}/labels`, { labels: [name] });
}

// Sets the approval status, merges, and withdraws the status if the merge does not happen,
// so an approval never outlives a failed merge.
async function approveAndMerge(api, number, sha, approver) {
  const setStatus = (state, description) =>
    api("POST", `/statuses/${sha}`, { state, context: APPROVAL_STATUS, description });
  await setStatus("success", `Approved by ${approver}`);
  try {
    // Passing the head sha makes the merge fail if a commit landed after the approval.
    // The branch rules can take a moment to see the new status, so retry a refused merge.
    for (let attempt = 1; ; attempt += 1) {
      try {
        await api("PUT", `/pulls/${number}/merge`, { merge_method: "squash", sha });
        return;
      } catch (error) {
        if (error.status !== 405 || attempt === MERGE_ATTEMPTS) throw error;
        await sleep(5000);
      }
    }
  } catch (error) {
    await setStatus("failure", "Approval withdrawn: the merge did not complete");
    throw error;
  }
}

async function main() {
  const { GITHUB_TOKEN, GITHUB_REPOSITORY, COMMENT_BODY, COMMENTER, COMMENTER_ID, COMMENTED_AT, PR_NUMBER } = process.env;
  const api = client(GITHUB_TOKEN, GITHUB_REPOSITORY);
  const comment = (message) => api("POST", `/issues/${PR_NUMBER}/comments`, { body: message });

  const parsed = parseCommand(COMMENT_BODY);
  const commenterId = Number(COMMENTER_ID);
  const stewards = JSON.parse(readFileSync(join(ROOT, ".github", "stewards.json"), "utf8")).stewards;
  const pull = await api("GET", `/pulls/${PR_NUMBER}`);
  const pr = { state: pull.state, draft: pull.draft, authorId: pull.user.id, headSha: pull.head.sha, changedFiles: pull.changed_files };

  const approving = parsed?.command === "approve" && isSteward(commenterId, stewards) && pr.state === "open";
  const { paths: changedPaths, listedFiles } = approving ? await changedPathsOf(api, PR_NUMBER) : { paths: [], listedFiles: 0 };
  const checkPassed = approving ? await requiredCheckPassed(api, pr.headSha) : false;
  const headArrivedAt = approving
    ? headArrival(await api("GET", `/commits/${pr.headSha}/statuses?per_page=${STATUSES_PER_PAGE}`), PR_NUMBER)
    : null;

  const decision = decide({
    parsed, commenterId, stewards, pr, changedPaths, listedFiles,
    checkPassed, headArrivedAt, commentedAt: Date.parse(COMMENTED_AT),
  });
  console.log(`decision: ${decision.action}${decision.why ? ` (${decision.why})` : ""}`);

  if (decision.action === "comment") {
    await comment(decision.message);
  } else if (decision.action === "reject") {
    const quoted = parsed.argument.split(/\r?\n/).map((line) => `> ${line}`).join("\n");
    await comment(`Declined by @${COMMENTER}.\n\n${quoted}\n\nThanks for contributing, @${pull.user.login}. You can open a new pull request once this is addressed.`);
    await addLabel(api, PR_NUMBER, "declined");
    await api("PATCH", `/pulls/${PR_NUMBER}`, { state: "closed" });
  } else if (decision.action === "approve") {
    try {
      await approveAndMerge(api, PR_NUMBER, pr.headSha, COMMENTER);
    } catch (error) {
      await comment(`The approval was recorded but the merge did not complete, so the approval was withdrawn. Check the pull request for conflicts or new commits, then \`/approve\` again.`);
      throw error;
    }
    await addLabel(api, PR_NUMBER, "approved");
    await comment(`Approved by @${COMMENTER} and merged at commit \`${pr.headSha}\`. Thanks, @${pull.user.login}: this is now a UiPath-approved community skill.`);
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    console.error(error.message);
    process.exit(1);
  });
}
