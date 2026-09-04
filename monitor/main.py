"""CLI entry point.

    python3 -m monitor.main                 # check + alert (what CI runs)
    python3 -m monitor.main --dry-run       # check only, never touches GitHub
    python3 -m monitor.main --site mr-corn  # one site
    python3 -m monitor.main --preflight     # verify alert permissions, no incident
    python3 -m monitor.main --json          # machine-readable results

Logging discipline: statuses, byte counts, timings and reason codes only.
Response bodies are never printed, so a page fragment can never end up in a
public Actions log.
"""

import argparse
import json
import os
import sys

from . import alerts, reasons
from .check import check_all
from .config import ALERT_LABEL, LABEL_DEFINITIONS, SITES, site_by_key

EXIT_OK = 0
EXIT_SITE_DOWN = 1
EXIT_MONITOR_ERROR = 2


def _status_icon(outcome):
    return "✅" if outcome.ok else "❌"


def summarise(outcome):
    status = outcome.http_status if outcome.http_status is not None else "-"
    if outcome.ok:
        extra = f"{outcome.score}/{outcome.total_groups} indicators"
    else:
        extra = outcome.detail
    return (
        f"{_status_icon(outcome)} {outcome.site_name:<18} "
        f"status={status} "
        f"reason={outcome.reason} "
        f"redirects={outcome.redirects} "
        f"bytes={outcome.body_bytes} "
        f"time={outcome.elapsed_ms}ms "
        f"attempts={outcome.attempts} "
        f"| {extra}"
    )


def step_summary_markdown(results, alert_actions, run_url):
    lines = [
        "## Uptime check",
        "",
        "| | Website | Status | Reason | Final URL | Redirects | Time | Attempts |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for outcome in results:
        status = outcome.http_status if outcome.http_status is not None else "—"
        lines.append(
            f"| {_status_icon(outcome)} | {outcome.site_name} | `{status}` | "
            f"`{outcome.reason}` | {outcome.final_url or outcome.url} | "
            f"{outcome.redirects} | {outcome.elapsed_ms} ms | {outcome.attempts} |"
        )

    failing = [o for o in results if not o.ok]
    if failing:
        lines += ["", "### Failure detail", ""]
        for outcome in failing:
            lines.append(f"- **{outcome.site_name}** — {outcome.reason_text}: {outcome.detail}")
    else:
        lines += ["", "All monitored websites responded correctly.", ""]

    if alert_actions:
        lines += ["", "### Alerting", ""]
        for action in alert_actions:
            target = f" (issue #{action.issue_number})" if action.issue_number else ""
            lines.append(f"- `{action.site_key}` → **{action.action}**{target} {action.detail}")

    if run_url:
        lines += ["", f"[Run link]({run_url})"]
    return "\n".join(lines) + "\n"


def write_step_summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return False
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)
        return True
    except OSError as exc:
        print(f"::warning::could not write job summary: {exc}", file=sys.stderr)
        return False


def resolve_assignee(env):
    owner = env.get("GITHUB_REPOSITORY_OWNER")
    if owner:
        return owner
    repository = env.get("GITHUB_REPOSITORY", "")
    return repository.split("/", 1)[0] if "/" in repository else None


def run_preflight(env):
    """Prove the alerting path works without inventing a fake outage."""
    print("Preflight: verifying alert permissions (no incident will be created)")
    try:
        client = alerts.client_from_env(env)
    except alerts.AlertError as exc:
        print(f"::error::preflight failed: {exc}")
        return EXIT_MONITOR_ERROR

    ok = True
    try:
        created = client.ensure_labels()
        present = client.existing_labels()
        missing = [name for name in LABEL_DEFINITIONS if name not in present]
        if created:
            print(f"  labels created (proves issues:write): {', '.join(created)}")
        else:
            print("  labels already present")
        if missing:
            print(f"::error::labels still missing: {', '.join(missing)}")
            ok = False
        else:
            print(f"  all {len(LABEL_DEFINITIONS)} labels present: {', '.join(sorted(LABEL_DEFINITIONS))}")
    except alerts.AlertError as exc:
        print(f"::error::label check failed (issues:write may be missing): {exc}")
        ok = False

    assignee = resolve_assignee(env)
    if assignee and client.is_assignable(assignee):
        print(f"  '{assignee}' is assignable — issue assignment will notify them")
    else:
        print(f"::error::'{assignee}' is NOT assignable on this repository")
        ok = False

    for site in SITES:
        try:
            issue = client.find_open_incident(site.label)
            state = f"open incident #{issue['number']}" if issue else "no open incident"
            print(f"  {site.name}: {state}")
        except alerts.AlertError as exc:
            print(f"::error::could not read issues for {site.name}: {exc}")
            ok = False

    print("Preflight PASSED" if ok else "Preflight FAILED")
    return EXIT_OK if ok else EXIT_MONITOR_ERROR


def run_notification_test(env):
    """Prove the phone-alert path end to end, without faking an outage.

    This exists because verifying the alert any other way is impossible:

      * Creating the issue yourself does NOT work - GitHub never notifies you
        about your own actions, so the notification is suppressed at source and
        the push layer is never exercised. (Measured: an issue created and
        self-assigned by the repository owner generated no notification thread
        at all.)
      * The only faithful test is one raised by the SAME actor a real incident
        uses - github-actions[bot], via the workflow's GITHUB_TOKEN.

    The issue carries no uptime label, so the incident logic never sees it.
    """
    print("Notification test: raising ONE test issue as github-actions[bot]")
    try:
        client = alerts.client_from_env(env)
    except alerts.AlertError as exc:
        print(f"::error::notification test failed: {exc}")
        return EXIT_MONITOR_ERROR

    assignee = resolve_assignee(env)
    if not assignee:
        print("::error::no repository owner resolved; cannot assign the test")
        return EXIT_MONITOR_ERROR

    run_url = alerts.run_url_from_env(env)
    moment = alerts.now_utc()
    body = "\n".join(
        [
            "**This is a notification test, not an incident.**",
            "",
            "It makes no claim about either website, and carries no "
            "`uptime-alert` label, so the monitor's incident logic cannot see it.",
            "",
            "| Field | Value |",
            "| --- | --- |",
            f"| Raised (UTC) | {alerts.format_utc(moment)} |",
            f"| Raised (UK) | {alerts.format_london(moment)} |",
            # Deliberately NOT an @mention. A real incident body contains no
            # @mention, so notifying via one would test a mechanism the real
            # alert never uses. This must notify by ASSIGNMENT alone, exactly
            # as a genuine outage does - GitHub reports that as reason=assign.
            f"| Assigned to | {assignee} |",
            "| Raised by | `github-actions[bot]` - the same actor a real incident uses |",
            "",
            f"[View the run]({run_url})" if run_url else "",
            "",
            "Close or delete this issue once the push notification has arrived.",
        ]
    )

    try:
        created = client.create_issue(
            f"🔔 Notification test - {alerts.format_utc(moment)}",
            body,
            [],
            [assignee],
        )
    except alerts.AlertError as exc:
        print(f"::error::could not raise the test issue: {exc}")
        return EXIT_MONITOR_ERROR

    number = (created or {}).get("number")
    actual = alerts._assignee_logins((created or {}).get("assignees"))
    print(f"  issue #{number} created, assigned to: {', '.join(actual) or 'nobody'}")
    if assignee not in actual:
        print(f"::warning::'{assignee}' was not assigned; a real alert may not notify")
        return EXIT_MONITOR_ERROR
    print("  Check the GitHub mobile app for a push notification now.")
    return EXIT_OK


def main(argv=None, env=None):
    env = env if env is not None else os.environ
    parser = argparse.ArgumentParser(description="Read-only uptime monitor (public HTTP GET only).")
    parser.add_argument("--dry-run", action="store_true", help="check only; never contact GitHub")
    parser.add_argument("--site", help="check a single site by key")
    parser.add_argument("--json", action="store_true", help="print results as JSON")
    parser.add_argument("--preflight", action="store_true", help="verify alert permissions and exit")
    parser.add_argument(
        "--notification-test",
        action="store_true",
        help="raise one test issue as the bot to prove the phone alert works",
    )
    args = parser.parse_args(argv)

    if args.preflight:
        return run_preflight(env)

    if args.notification_test:
        return run_notification_test(env)

    if args.site:
        site = site_by_key(args.site)
        if site is None:
            print(f"unknown site '{args.site}'; known: {', '.join(s.key for s in SITES)}", file=sys.stderr)
            return EXIT_MONITOR_ERROR
        sites = (site,)
    else:
        sites = SITES

    results = check_all(sites)

    # In --json mode stdout must contain NOTHING but the JSON payload, or
    # consumers that pipe it into a parser get a syntax error. Human-readable
    # lines go to stderr instead.
    log = sys.stderr if args.json else sys.stdout

    for outcome in results:
        print(summarise(outcome), file=log)

    alert_actions = []
    if args.dry_run:
        print("\n[dry-run] GitHub was not contacted; no issue was created, commented on or closed.", file=log)
    else:
        run_url = alerts.run_url_from_env(env)
        assignee = resolve_assignee(env)
        try:
            client = alerts.client_from_env(env)
            client.ensure_labels()
        except alerts.AlertError as exc:
            print(f"::warning::alerting unavailable ({exc}); check results above are still valid", file=log)
            client = None
        if client is not None:
            # Pair each outcome with its own site by key rather than by
            # position, so a future change to check_all cannot silently file an
            # incident against the wrong website.
            by_key = {site.key: site for site in sites}
            for outcome in results:
                site = by_key.get(outcome.site_key)
                if site is None:  # pragma: no cover - defensive
                    print(f"::warning::no site config for '{outcome.site_key}'; skipping alert", file=log)
                    continue
                try:
                    action = alerts.reconcile(outcome, site, client, assignee=assignee, run_url=run_url)
                    alert_actions.append(action)
                    print(f"alert[{site.key}]: {action.action} {action.detail}", file=log)
                except alerts.AlertError as exc:
                    print(f"::warning::alerting failed for {site.key}: {exc}", file=log)

    if args.json:
        print(json.dumps([vars(outcome) for outcome in results], indent=2, default=str))

    run_url = alerts.run_url_from_env(env)
    write_step_summary(step_summary_markdown(results, alert_actions, run_url))

    down = [o for o in results if not o.ok]
    if down:
        for outcome in down:
            print(f"::error title={outcome.site_name} is down::{outcome.reason} — {outcome.reason_text}", file=log)
        return EXIT_SITE_DOWN
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
