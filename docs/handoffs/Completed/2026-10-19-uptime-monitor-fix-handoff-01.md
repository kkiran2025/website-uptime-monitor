---
handoff_version: 2
handoff_id: "2026-10-19-01"
workstream: "uptime-monitor-fix"
title: "Uptime monitor fix — Step 5: measure the 5-minute cadence, then document it"
status: "completed"
priority: 1
created_at: "2026-10-19T15:21:52+01:00"
updated_at: "2026-10-19T15:21:52+01:00"
source_branch: "main"
source_commit: "73caee1"
depends_on: []
supersedes: []
next_action: "After 13:55 London on 2026-10-20, measure 24 h of 'Uptime monitor' runs since 2026-10-19T12:55:00Z with gh run list."
---

> ✅ Completed: 2026-10-21

# Uptime monitor fix — Step 5 handoff

## Why this exists

The monitor promised checks "about every five minutes", but GitHub's own schedule delivered only
4–9 runs a day (typical gap ~3 h 50 min, measured over 274 runs, 2026-09-04 → 2026-10-19), and the
repository was heading for GitHub's 60-day inactivity switch-off around 2026-11-03. A five-step
plan was approved by the owner on 2026-10-19. Steps 1–4 are shipped; only Step 5 remains.

## State

| Step | Status | Evidence |
|---|---|---|
| 1. README states the real cadence + keep-alive dates | ✅ shipped | `37d77fc` on `origin/main` |
| 2. Keep-alive reminder issue (45 days after `pushed_at`) | ✅ shipped | `73caee1` on `origin/main`; Codex 3 rounds, final all PASS; live run logged `alert[keepalive]: healthy last push 0 days ago` |
| 3. Owner chose the fast-trigger option | ✅ | cron-job.org → GitHub `workflow_dispatch` |
| 4. Owner set up cron-job.org + limited GitHub key | ✅ | First self-started run 2026-10-19 12:55 UTC, success; 17 runs in the first 75 min, max gap 5.6 min |
| 5. Measure 24 h, then document | ⏳ open | Measurement window ends 13:55 London, 2026-10-20 |

**How the trigger works now:** a cron-job.org job (every 5 min, `*/5`, Europe/London) sends
`POST https://api.github.com/repos/kkiran2025/website-uptime-monitor/actions/workflows/uptime-monitor.yml/dispatches`
with body `{"ref":"main"}`. It uses a fine-grained personal access token named
`uptime-monitor-cron-job-org`: this repository only, **Actions: read and write** (+ required
Metadata: read-only), **expires 2027-10-18**. The token is stored **only** in cron-job.org — never
ask the owner for it and never write it anywhere. cron-job.org emails the owner after 3 consecutive
failures, on recovery, and if the job is auto-disabled. GitHub's own `cron` schedule stays in the
workflow as a fallback.

## Step 5 — what to do

1. After 13:55 London on 2026-10-20, measure exactly this window — **2026-10-19T12:55:00Z to
   2026-10-20T12:55:00Z** — filtered on GitHub's side, so runs made after the window can never
   push part of it out of the result limit, however late this is resumed:
   `gh run list --workflow "Uptime monitor" --created "2026-10-19T12:55:00Z..2026-10-20T12:55:00Z" --limit 1000 --json createdAt,event,conclusion`
   Confirm the result count is below the limit (otherwise raise it and re-run).
   **Done when:** at least 250 runs, no gap longer than ~10 min, and every failure explained.
2. Update `README.md`: the "Scheduled runs are far less frequent…" section becomes a description
   of the cron-job.org trigger with the measured figures; add the token renewal duty (renew before
   **2027-10-18**: create a new token with the same scope, paste it into the cron-job.org job's
   `Authorization: Bearer …` header, run TEST RUN, expect `204`, then delete the old token).
   Also correct the `.github/workflows/` line in the Layout block, which still calls the schedule
   "the every-5-minutes check" — that line sits in a fenced block, so the change becomes Tier 2
   and owes a Codex review.
3. Tests (`python3 -m unittest discover -s tests -t .`), gitleaks, then ask the owner before the
   commit + push to `main` (single-branch repository: the push is live).
4. When Step 5 ships, this handoff is complete: run the archive procedure and ask the owner.

## ⏱ TIME

```
⏱ TIME — Uptime monitor fix
Estimate:    2–3 h active + a 24-hour measurement wait (original, 2026-10-19)
Spent:       34m active, measured on 2026-10-19 (step-status.sh; owner click-time excluded)
Remaining:   20–30 min — 4 of 5 steps verified
Finish:      2026-10-20, after 13:55 London, assuming the owner answers the push gate the same day
Confidence:  HIGH — the first 75 minutes already met the target
```

## Things worth knowing

- **Some local networks block MR Corn.** A network-level web filter can serve its own
  "blocked page" certificate, so a local `python3 -m monitor.main --dry-run` may show a false
  MR Corn `tls_failure`. GitHub runners are unaffected; always judge the sites from Actions runs.
- **A Node.js warning is cosmetic.** `actions/checkout@v4` triggers a Node 20 deprecation
  warning; it is not a failure and is out of scope for this workstream.
- **Keep-alive dates.** The next push is due by 2026-12-08 (hard cut-off ~2026-12-18), but the
  Step 2 reminder will raise an issue at 45 days anyway, and the Step 5 push itself resets the
  clock.

## ✂ Resume prompt (paste as the first message of a new chat)

```
Continuing the uptime monitor fix in ~/projects/website-uptime-monitor (repo
kkiran2025/website-uptime-monitor, single branch: main).

Read docs/handoffs/2026-10-19-uptime-monitor-fix-handoff-01.md first. Steps 1-4 are shipped
(37d77fc, 73caee1; cron-job.org now dispatches the "Uptime monitor" workflow every 5 minutes).

Do Step 5 only: measure the 24 h of runs since 2026-10-19T12:55:00Z (target >= 250 runs, max
gap ~10 min), update README.md with the new trigger and the token renewal date (2027-10-18),
run the tests and gitleaks, then STOP and ask me before committing and pushing to main.
Never ask me for the GitHub token value.
```

Resume command: `/handoff-next docs/handoffs/2026-10-19-uptime-monitor-fix-handoff-01.md`

**CLOSED 2026-10-21** — commit `a9519c6` on the production branch.
