# Reviewing a summary before it is stored

An AI summary is the only part of the index a language model writes, so it gets a second, adversarial pass. The reviewer's job is to **disprove** each claim, not to agree with it. `summary set` refuses to store a summary unless a review file is supplied and every claim in it is `SUPPORTED`.

## Method

1. **Split the summary into claims.** One row per factual statement: "reads invoices from the Invoices queue", "logs in to the supplier portal", "runs every weekday". Opinions and phrasing don't need rows; anything a reader could act on does.
2. **Look for evidence against each claim**, then for evidence for it:
   - Query the index: `edge`, `v_process_edge`, `v_process_link`, `job_summary`, `orch_trigger`, `activity_use`.
   - Read the source tree and cite `file:line`.
   - A claim about something the index cannot see (business purpose, who uses the output) is `UNSUPPORTED` unless a document in the source tree says it.
3. **Give a verdict per claim:**
   - `SUPPORTED`: the evidence shows it. Cite the query or `file:line`.
   - `UNSUPPORTED`: nothing shows it either way.
   - `CONTRADICTED`: the evidence shows something else. Say what.
4. **Fix the summary.** Remove or correct every `UNSUPPORTED` and `CONTRADICTED` claim, then review the new version. Only a review where every row is `SUPPORTED` is accepted.

## Review file format

Plain Markdown, one row per claim:

```markdown
| Claim | Verdict | Evidence |
|---|---|---|
| Adds one item per invoice to the Invoices queue | SUPPORTED | edge uses_queue invoices mode=add, Process/AddItems.xaml:41 |
| Reads the supplier portal | SUPPORTED | uses_application msedge.exe + targets_url portal.example.com, Main.xaml:88 |
| Runs every weekday at 07:00 | SUPPORTED | orch_trigger time "Weekday 7AM", enabled |
```

## Store it

```bash
node SKILL_DIR/scripts/automation-index.mjs --index-dir INDEX_DIR summary set --package ID --version V --model MODEL_NAME --file summary.md --review review.md
```

The review is stored next to the summary. Cards show how many claims were checked, and mark the summary stale when the package changes.
