"""Incident lifecycle, expressed entirely as GitHub Issues.

The open issue *is* the state. Nothing is written back to the repository, which
is why the workflow can run with `contents: read`. One incident per outage:

    fail  + no open issue   -> create one, assigned to the owner (push notification)
    fail  + open issue      -> stay silent, unless the failure CLASS changed
    healthy + open issue    -> one recovery comment with the duration, then close
    healthy + no open issue -> do nothing

A later, separate outage therefore opens a brand-new issue, because the previous
one was closed.

Note on content: this repository is public, so issue bodies are deliberately
terse - site, URL, timestamps, status code, reason class. No page titles, no
stack traces, no file paths, no server internals, no response bodies.
"""

import datetime
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import reasons
from .config import ALERT_LABEL, LABEL_DEFINITIONS

try:
    from zoneinfo import ZoneInfo

    LONDON = ZoneInfo("Europe/London")
except Exception:  # pragma: no cover - zoneinfo data always present on runners
    LONDON = None

#: Hidden marker so we can tell whether the failure class has changed without
#: keeping any state outside GitHub.
_MARKER_PREFIX = "<!-- monitor:reason="
_MARKER_SUFFIX = " -->"

API_TIMEOUT_SECONDS = 20


class AlertError(RuntimeError):
    pass


@dataclass
class AlertAction:
    site_key: str
    action: str  # created | commented | resolved | unchanged | healthy | skipped
    issue_number: int = None
    detail: str = ""


# --- Time helpers -----------------------------------------------------------


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def format_utc(moment) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S UTC")


def format_london(moment) -> str:
    if LONDON is None:  # pragma: no cover
        return "unavailable"
    local = moment.astimezone(LONDON)
    return local.strftime("%Y-%m-%d %H:%M:%S %Z (Europe/London)")


def format_duration(delta) -> str:
    total = int(delta.total_seconds())
    if total < 0:
        return "unknown"
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def parse_github_timestamp(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# --- Marker helpers ---------------------------------------------------------


def build_marker(reason: str) -> str:
    return f"{_MARKER_PREFIX}{reason}{_MARKER_SUFFIX}"


def read_marker(body: str):
    if not body or _MARKER_PREFIX not in body:
        return None
    start = body.index(_MARKER_PREFIX) + len(_MARKER_PREFIX)
    end = body.find(_MARKER_SUFFIX, start)
    if end == -1:
        return None
    return body[start:end].strip() or None


# --- GitHub client ----------------------------------------------------------


class GitHubClient:
    """Thin GitHub REST client over urllib - no third-party dependencies."""

    def __init__(self, token, repository, api_url="https://api.github.com", opener=None):
        if not token:
            raise AlertError("no GITHUB_TOKEN available")
        if not repository:
            raise AlertError("no GITHUB_REPOSITORY available")
        self.token = token
        self.repository = repository
        self.api_url = api_url.rstrip("/")
        self._opener = opener or urllib.request.build_opener()

    def _request(self, method, path, payload=None):
        url = path if path.startswith("http") else f"{self.api_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", "website-uptime-monitor")
        if data is not None:
            request.add_header("Content-Type", "application/json")

        try:
            with self._opener.open(request, timeout=API_TIMEOUT_SECONDS) as response:
                raw = response.read()
                status = getattr(response, "status", response.getcode())
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:  # pragma: no cover
                pass
            raise AlertError(f"GitHub API {method} {path} -> HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise AlertError(f"GitHub API {method} {path} unreachable: {exc.reason}") from exc
        except OSError as exc:
            # response.read() can raise socket.timeout / ConnectionResetError,
            # which are OSError but NOT URLError, so the clause above misses
            # them. Unwrapped, they would escape main()'s `except AlertError`
            # and abort the whole run - meaning a GitHub API hiccup would look
            # like a site outage and the job summary would never be written.
            raise AlertError(
                f"GitHub API {method} {path} transport error: {type(exc).__name__}: {exc}"
            ) from exc

        if not raw:
            return status, None
        try:
            return status, json.loads(raw.decode("utf-8"))
        except ValueError:
            return status, None

    # --- labels -------------------------------------------------------------

    def existing_labels(self):
        _, data = self._request("GET", f"/repos/{self.repository}/labels?per_page=100")
        return {item["name"] for item in (data or [])}

    def ensure_labels(self):
        """Create any missing labels. Also proves `issues: write` really works."""
        present = self.existing_labels()
        created = []
        for name, (colour, description) in LABEL_DEFINITIONS.items():
            if name in present:
                continue
            self._request(
                "POST",
                f"/repos/{self.repository}/labels",
                {"name": name, "color": colour, "description": description},
            )
            created.append(name)
        return created

    # --- assignability ------------------------------------------------------

    def is_assignable(self, login):
        try:
            status, _ = self._request("GET", f"/repos/{self.repository}/assignees/{login}")
            return status == 204
        except AlertError:
            return False

    # --- issues -------------------------------------------------------------

    def find_open_incident(self, site_label):
        labels = urllib.parse.quote(f"{ALERT_LABEL},{site_label}")
        _, data = self._request(
            "GET",
            f"/repos/{self.repository}/issues?state=open&labels={labels}&per_page=20",
        )
        for issue in data or []:
            if "pull_request" in issue:
                continue
            return issue
        return None

    def create_issue(self, title, body, labels, assignees):
        payload = {"title": title, "body": body, "labels": list(labels)}
        if assignees:
            payload["assignees"] = list(assignees)
        try:
            _, data = self._request("POST", f"/repos/{self.repository}/issues", payload)
            return data
        except AlertError:
            if not assignees:
                raise
            # GitHub rejects the WHOLE request with 422 if an assignee is not
            # assignable - and an organisation slug never is. That would mean no
            # incident issue at all, i.e. a completely missed alert. The alert
            # matters more than the assignment, so drop it and try again.
            payload.pop("assignees", None)
            _, data = self._request("POST", f"/repos/{self.repository}/issues", payload)
            return data

    def add_comment(self, number, body):
        return self._request("POST", f"/repos/{self.repository}/issues/{number}/comments", {"body": body})

    def update_issue(self, number, payload):
        return self._request("PATCH", f"/repos/{self.repository}/issues/{number}", payload)

    def close_issue(self, number):
        return self.update_issue(number, {"state": "closed", "state_reason": "completed"})


# --- Message building -------------------------------------------------------


def incident_title(outcome) -> str:
    return f"🔴 {outcome.site_name} is DOWN — {outcome.reason}"


def incident_body(outcome, detected_at, run_url) -> str:
    status = outcome.http_status if outcome.http_status is not None else "no response"
    lines = [
        build_marker(outcome.reason),
        f"**{outcome.site_name}** failed an automated availability check.",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Website | {outcome.site_name} |",
        f"| URL | {outcome.url} |",
        f"| Detected (UTC) | {format_utc(detected_at)} |",
        f"| Detected (UK) | {format_london(detected_at)} |",
        f"| HTTP status | {status} |",
        f"| Final URL | {outcome.final_url or outcome.url} |",
        f"| Redirects | {outcome.redirects} |",
        f"| Attempts | {outcome.attempts} |",
        f"| Reason | `{outcome.reason}` — {outcome.reason_text} |",
        "",
        f"**Detail:** {outcome.detail}",
        "",
        f"[View the monitoring run]({run_url})" if run_url else "",
        "",
        "---",
        "_This issue closes automatically when the site passes its next check._",
    ]
    return "\n".join(line for line in lines if line is not None)


def recovery_comment(outcome, recovered_at, opened_at, run_url) -> str:
    if opened_at is not None:
        duration = format_duration(recovered_at - opened_at)
        duration_line = f"| Outage duration | approximately {duration} |"
    else:
        duration_line = "| Outage duration | could not be determined |"

    lines = [
        f"✅ **{outcome.site_name} is back up.**",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Recovered (UTC) | {format_utc(recovered_at)} |",
        f"| Recovered (UK) | {format_london(recovered_at)} |",
        f"| HTTP status | {outcome.http_status} |",
        f"| Storefront checks | {outcome.score}/{outcome.total_groups} indicator groups present |",
        duration_line,
        "",
        f"[View the recovery run]({run_url})" if run_url else "",
        "",
        "Closing this incident.",
    ]
    return "\n".join(line for line in lines if line is not None)


def reason_change_comment(outcome, previous_reason, moment, run_url) -> str:
    return "\n".join(
        line
        for line in [
            f"⚠️ The failure changed from `{previous_reason}` to `{outcome.reason}`.",
            "",
            f"- **When (UTC):** {format_utc(moment)}",
            f"- **When (UK):** {format_london(moment)}",
            f"- **Now:** {outcome.reason_text}",
            f"- **Detail:** {outcome.detail}",
            "",
            f"[View the run]({run_url})" if run_url else "",
        ]
        if line is not None
    )


# --- Reconciliation ---------------------------------------------------------



def _assignee_logins(value):
    """Extract logins from an assignees field.

    GitHub returns user objects (``{"login": ...}``). Being tolerant of a bare
    string costs nothing and matters here: an AttributeError raised while
    filing an incident would lose the alert entirely, which is the one outcome
    this whole project exists to prevent.
    """
    logins = []
    for item in value or []:
        if isinstance(item, dict):
            logins.append(item.get("login"))
        elif isinstance(item, str):
            logins.append(item)
    return logins


def reconcile(outcome, site, client, assignee=None, run_url="", moment=None):
    """Bring the GitHub issue state in line with one check outcome."""
    moment = moment or now_utc()
    issue = client.find_open_incident(site.label)

    if outcome.ok:
        if issue is None:
            return AlertAction(site.key, "healthy")
        opened_at = parse_github_timestamp(issue.get("created_at"))
        client.add_comment(issue["number"], recovery_comment(outcome, moment, opened_at, run_url))
        client.close_issue(issue["number"])
        return AlertAction(site.key, "resolved", issue["number"], "recovery comment posted, issue closed")

    if issue is None:
        assignees = [assignee] if assignee else []
        created = client.create_issue(
            incident_title(outcome),
            incident_body(outcome, moment, run_url),
            site.issue_labels,
            assignees,
        )
        number = (created or {}).get("number")
        actual = _assignee_logins((created or {}).get("assignees"))
        if assignee and assignee not in actual:
            detail = (
                f"created WITHOUT an assignee - '{assignee}' is not assignable on this "
                "repository, so GitHub may not send a notification"
            )
        else:
            detail = f"assigned to {assignee or 'nobody'}"
        return AlertAction(site.key, "created", number, detail)

    previous = read_marker(issue.get("body") or "")
    if previous and previous != outcome.reason:
        client.add_comment(issue["number"], reason_change_comment(outcome, previous, moment, run_url))
        new_body = (issue.get("body") or "").replace(build_marker(previous), build_marker(outcome.reason), 1)
        client.update_issue(issue["number"], {"body": new_body, "title": incident_title(outcome)})
        return AlertAction(site.key, "commented", issue["number"], f"reason changed {previous} -> {outcome.reason}")

    return AlertAction(site.key, "unchanged", issue["number"], "incident already open; staying quiet")


# --- Environment helpers ----------------------------------------------------


def run_url_from_env(env=None):
    env = env if env is not None else os.environ
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    repo = env.get("GITHUB_REPOSITORY", "")
    run_id = env.get("GITHUB_RUN_ID", "")
    if not repo or not run_id:
        return ""
    return f"{server}/{repo}/actions/runs/{run_id}"


def client_from_env(env=None):
    env = env if env is not None else os.environ
    return GitHubClient(
        token=env.get("GITHUB_TOKEN", ""),
        repository=env.get("GITHUB_REPOSITORY", ""),
        api_url=env.get("GITHUB_API_URL", "https://api.github.com"),
    )
