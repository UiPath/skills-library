#!/usr/bin/env node
// Handles the steward commands typed as a pull request comment:
//   /approve            merge the pull request (squash)
//   /reject <reason>    post the reason, label and close the pull request
// Run by .github/workflows/steward-commands.yml from the default branch.

import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const REQUIRED_CHECK = "Validate skills";
// Required status on main. Only this workflow sets it, so a pull request cannot merge without /approve.
const APPROVAL_STATUS = "Steward approval";
const MERGE_ATTEMPTS = 4;
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
const LABELS = {
  approved: { color: "1f8a5b", description: "Approved by a UiPath steward" },
  declined: { color: "b7791f", description: "Declined by a UiPath steward, with a reason" },
};

// The command must be the first word of the comment. Anything after /reject is the reason.
export function parseCommand(body) {
  const match = /^\/(approve|reject)(?:[ \t]+([\s\S]*))?$/.exec(String(body ?? "").trim());
  if (!match) return null;
  return { command: match[1], reason: (match[2] ?? "").trim() };
}

export function isSteward(login, logins) {
  const wanted = String(login ?? "").toLowerCase();
  return logins.some((entry) => entry.toLowerCase() === wanted);
}

// Decides what to do. Pure, so it can be tested without GitHub.
export function decide({ parsed, commenter, stewards, pr, checkPassed }) {
  if (!parsed) return { action: "ignore", why: "no steward command" };
  if (!isSteward(commenter, stewards)) return { action: "ignore", why: `${commenter} is not a steward` };
  if (pr.state !== "open") return { action: "comment", message: "This pull request is not open, so there is nothing to approve or decline." };

  if (parsed.command === "reject") {
    if (!parsed.reason) {
      return { action: "comment", message: "A reason is required. Use `/reject <reason>` so the contributor knows what to change or where to go instead." };
    }
    return { action: "reject" };
  }

  if (isSteward(pr.author, [commenter])) {
    return { action: "comment", message: "Stewards cannot `/approve` their own pull request. Ask another steward." };
  }
  if (pr.draft) return { action: "comment", message: "This pull request is still a draft. Mark it ready for review first." };
  if (!checkPassed) {
    return { action: "comment", message: `Cannot approve yet: the \`${REQUIRED_CHECK}\` check has not passed on the latest commit.` };
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

async function addLabel(api, number, name) {
  try {
    await api("POST", "/labels", { name, ...LABELS[name] });
  } catch (error) {
    if (error.status !== 422) throw error; // 422: the label already exists
  }
  await api("POST", `/issues/${number}/labels`, { labels: [name] });
}

async function main() {
  const { GITHUB_TOKEN, GITHUB_REPOSITORY, COMMENT_BODY, COMMENTER, PR_NUMBER } = process.env;
  const api = client(GITHUB_TOKEN, GITHUB_REPOSITORY);
  const comment = (message) => api("POST", `/issues/${PR_NUMBER}/comments`, { body: message });

  const parsed = parseCommand(COMMENT_BODY);
  const stewards = JSON.parse(readFileSync(join(ROOT, ".github", "stewards.json"), "utf8")).logins;
  const pull = await api("GET", `/pulls/${PR_NUMBER}`);
  const pr = { state: pull.state, draft: pull.draft, author: pull.user.login };

  let checkPassed = false;
  if (parsed?.command === "approve") {
    const runs = await api("GET", `/commits/${pull.head.sha}/check-runs?check_name=${encodeURIComponent(REQUIRED_CHECK)}&filter=latest`);
    checkPassed = runs.check_runs.length > 0 && runs.check_runs.every((run) => run.conclusion === "success");
  }

  const decision = decide({ parsed, commenter: COMMENTER, stewards, pr, checkPassed });
  console.log(`decision: ${decision.action}${decision.why ? ` (${decision.why})` : ""}`);

  if (decision.action === "comment") {
    await comment(decision.message);
  } else if (decision.action === "reject") {
    const quoted = parsed.reason.split(/\r?\n/).map((line) => `> ${line}`).join("\n");
    await comment(`Declined by @${COMMENTER}.\n\n${quoted}\n\nThanks for contributing, @${pr.author}. You can open a new pull request once this is addressed.`);
    await addLabel(api, PR_NUMBER, "declined");
    await api("PATCH", `/pulls/${PR_NUMBER}`, { state: "closed" });
  } else if (decision.action === "approve") {
    await api("POST", `/statuses/${pull.head.sha}`, {
      state: "success",
      context: APPROVAL_STATUS,
      description: `Approved by ${COMMENTER}`,
    });
    // Passing the head sha makes the merge fail if a commit landed after the check ran.
    // The branch rules can take a moment to see the new status, so retry a refused merge.
    for (let attempt = 1; ; attempt += 1) {
      try {
        await api("PUT", `/pulls/${PR_NUMBER}/merge`, { merge_method: "squash", sha: pull.head.sha });
        break;
      } catch (error) {
        if (error.status !== 405 || attempt === MERGE_ATTEMPTS) throw error;
        await sleep(5000);
      }
    }
    await addLabel(api, PR_NUMBER, "approved");
    await comment(`Approved by @${COMMENTER} and merged. Thanks, @${pr.author}: this is now a UiPath-approved community skill.`);
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    console.error(error.message);
    process.exit(1);
  });
}
