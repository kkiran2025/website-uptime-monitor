"""Incident-lifecycle tests. Offline: no GitHub API call is ever made."""

import datetime
import json
import socket
import unittest

from monitor import alerts, reasons
from monitor.check import CheckOutcome
from monitor.config import site_by_key
from tests import fakes
from tests.fakes import FakeGitHubClient

DP = site_by_key("direct-packaging")
RUN_URL = "https://github.com/kkiran2025/website-uptime-monitor/actions/runs/123"
MOMENT = datetime.datetime(2026, 7, 15, 13, 30, 0, tzinfo=datetime.timezone.utc)


def down(reason=reasons.HTTP_STATUS, status=503, detail="final response was HTTP 503"):
    return CheckOutcome(
        site_key=DP.key,
        site_name=DP.name,
        url=DP.url,
        ok=False,
        reason=reason,
        detail=detail,
        http_status=status,
        final_url=DP.url,
        attempts=3,
    )


def up():
    return CheckOutcome(
        site_key=DP.key,
        site_name=DP.name,
        url=DP.url,
        ok=True,
        reason=reasons.OK,
        detail="4/4 indicator groups present",
        http_status=200,
        final_url=DP.url,
        score=4,
        total_groups=4,
    )


def open_issue(reason=reasons.HTTP_STATUS, created_at="2026-07-15T12:00:00Z"):
    return {
        "number": 7,
        "title": "🔴 Direct Packaging is DOWN — http_status",
        "body": alerts.build_marker(reason) + "\nbody text",
        "created_at": created_at,
    }


class TestHelpers(unittest.TestCase):
    def test_marker_round_trip(self):
        self.assertEqual(alerts.read_marker(alerts.build_marker("timeout")), "timeout")

    def test_marker_absent(self):
        self.assertIsNone(alerts.read_marker("no marker here"))
        self.assertIsNone(alerts.read_marker(""))

    def test_duration_formatting(self):
        self.assertEqual(alerts.format_duration(datetime.timedelta(seconds=45)), "45s")
        self.assertEqual(alerts.format_duration(datetime.timedelta(minutes=7, seconds=3)), "7m 3s")
        self.assertEqual(alerts.format_duration(datetime.timedelta(hours=2, minutes=5)), "2h 5m")

    def test_timestamp_parsing(self):
        parsed = alerts.parse_github_timestamp("2026-07-15T12:00:00Z")
        self.assertEqual(parsed.year, 2026)
        self.assertIsNone(alerts.parse_github_timestamp("not a date"))
        self.assertIsNone(alerts.parse_github_timestamp(None))

    def test_london_time_is_bst_in_july(self):
        self.assertIn("BST", alerts.format_london(MOMENT))

    def test_london_time_is_gmt_in_january(self):
        winter = datetime.datetime(2026, 1, 15, 13, 30, tzinfo=datetime.timezone.utc)
        self.assertIn("GMT", alerts.format_london(winter))

    def test_run_url_from_env(self):
        env = {
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_REPOSITORY": "kkiran2025/website-uptime-monitor",
            "GITHUB_RUN_ID": "999",
        }
        self.assertEqual(
            alerts.run_url_from_env(env),
            "https://github.com/kkiran2025/website-uptime-monitor/actions/runs/999",
        )
        self.assertEqual(alerts.run_url_from_env({}), "")


class TestIncidentBody(unittest.TestCase):
    def setUp(self):
        self.body = alerts.incident_body(down(), MOMENT, RUN_URL)

    def test_contains_every_required_field(self):
        for required in [
            "Direct Packaging",
            DP.url,
            "2026-07-15 13:30:00 UTC",
            "Europe/London",
            "503",
            "http_status",
            RUN_URL,
        ]:
            with self.subTest(field=required):
                self.assertIn(required, self.body)

    def test_uk_time_is_one_hour_ahead_in_july(self):
        self.assertIn("14:30:00 BST", self.body)

    def test_carries_a_reason_marker(self):
        self.assertEqual(alerts.read_marker(self.body), reasons.HTTP_STATUS)

    def test_does_not_leak_server_internals(self):
        """The repository is public, so bodies must stay terse."""
        leaky = down(
            reason=reasons.ERROR_PAGE,
            status=200,
            detail="PHP fatal error (matched in head; page 812 bytes, storefront score 0/4)",
        )
        body = alerts.incident_body(leaky, MOMENT, RUN_URL)
        for forbidden in ["/home/", "wp-content", "Stack trace", "<html", "Uncaught Error"]:
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, body)


class TestReconcile(unittest.TestCase):
    def test_first_failure_creates_an_assigned_issue(self):
        client = FakeGitHubClient(open_issue=None)
        action = alerts.reconcile(down(), DP, client, assignee="kkiran2025", run_url=RUN_URL, moment=MOMENT)

        self.assertEqual(action.action, "created")
        self.assertEqual(len(client.created), 1)
        issue = client.created[0]
        self.assertEqual([a["login"] for a in issue["assignees"]], ["kkiran2025"])
        self.assertIn("uptime-alert", issue["labels"])
        self.assertIn("uptime-direct-packaging", issue["labels"])
        self.assertEqual(client.comments, [])

    def test_an_unassignable_owner_still_gets_an_issue(self):
        """GitHub 422s the WHOLE request for a bad assignee (e.g. an org slug).

        Losing the assignment is acceptable. Losing the incident is not.
        """
        client = FakeGitHubClient(open_issue=None, reject_assignees=True)
        action = alerts.reconcile(down(), DP, client, assignee="some-org", run_url=RUN_URL, moment=MOMENT)

        self.assertEqual(action.action, "created")
        self.assertEqual(len(client.created), 1, "the incident must still be filed")
        self.assertEqual(client.created[0]["assignees"], [])
        self.assertIn("WITHOUT an assignee", action.detail)
        self.assertIn("not assignable", action.detail)

    def test_a_successful_assignment_is_reported_as_such(self):
        client = FakeGitHubClient(open_issue=None)
        action = alerts.reconcile(down(), DP, client, assignee="kkiran2025", run_url=RUN_URL, moment=MOMENT)
        self.assertIn("assigned to kkiran2025", action.detail)

    def test_continuing_outage_stays_silent(self):
        client = FakeGitHubClient(open_issue=open_issue())
        action = alerts.reconcile(down(), DP, client, assignee="kkiran2025", run_url=RUN_URL, moment=MOMENT)

        self.assertEqual(action.action, "unchanged")
        self.assertEqual(client.created, [])
        self.assertEqual(client.comments, [], "an ongoing outage must not be commented on repeatedly")
        self.assertEqual(client.closed, [])

    def test_twelve_consecutive_failures_produce_exactly_one_issue(self):
        client = FakeGitHubClient(open_issue=None)
        for _ in range(12):
            alerts.reconcile(down(), DP, client, assignee="kkiran2025", run_url=RUN_URL, moment=MOMENT)
            client.open_issue = client.open_issue or open_issue()
        self.assertEqual(len(client.created), 1)
        self.assertEqual(len(client.comments), 0)

    def test_reason_change_produces_one_comment(self):
        client = FakeGitHubClient(open_issue=open_issue(reason=reasons.TIMEOUT))
        action = alerts.reconcile(down(), DP, client, assignee="kkiran2025", run_url=RUN_URL, moment=MOMENT)

        self.assertEqual(action.action, "commented")
        self.assertEqual(len(client.comments), 1)
        self.assertIn("timeout", client.comments[0][1])
        self.assertIn("http_status", client.comments[0][1])
        updated_body = client.updates[0][1]["body"]
        self.assertEqual(alerts.read_marker(updated_body), reasons.HTTP_STATUS)

    def test_recovery_comments_then_closes(self):
        client = FakeGitHubClient(open_issue=open_issue(created_at="2026-07-15T12:00:00Z"))
        action = alerts.reconcile(up(), DP, client, assignee="kkiran2025", run_url=RUN_URL, moment=MOMENT)

        self.assertEqual(action.action, "resolved")
        self.assertEqual(len(client.comments), 1)
        self.assertIn("back up", client.comments[0][1])
        self.assertIn("1h 30m", client.comments[0][1], "outage duration must be reported")
        self.assertEqual(client.closed, [7])

    def test_recovery_without_a_reliable_start_time(self):
        client = FakeGitHubClient(open_issue=open_issue(created_at="garbage"))
        alerts.reconcile(up(), DP, client, run_url=RUN_URL, moment=MOMENT)
        self.assertIn("could not be determined", client.comments[0][1])

    def test_healthy_with_no_open_issue_does_nothing(self):
        client = FakeGitHubClient(open_issue=None)
        action = alerts.reconcile(up(), DP, client, assignee="kkiran2025", run_url=RUN_URL, moment=MOMENT)

        self.assertEqual(action.action, "healthy")
        self.assertEqual(client.created, [])
        self.assertEqual(client.comments, [])
        self.assertEqual(client.closed, [])

    def test_a_later_outage_opens_a_new_issue(self):
        """After recovery the issue is closed, so find_open_incident returns None."""
        client = FakeGitHubClient(open_issue=open_issue())
        alerts.reconcile(up(), DP, client, run_url=RUN_URL, moment=MOMENT)
        client.open_issue = None  # the previous incident is now closed
        action = alerts.reconcile(down(), DP, client, assignee="kkiran2025", run_url=RUN_URL, moment=MOMENT)
        self.assertEqual(action.action, "created")
        self.assertEqual(len(client.created), 1)


class TestTransportErrorWrapping(unittest.TestCase):
    """A GitHub API hiccup must degrade gracefully, never abort the run."""

    class _ExplodingResponse:
        status = 200
        headers = None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            # socket.timeout is an OSError but NOT a urllib URLError, so an
            # unwrapped one would escape main()'s `except AlertError`.
            raise socket.timeout("timed out reading the response")

    class _ExplodingOpener:
        def open(self, request, timeout=None):
            return TestTransportErrorWrapping._ExplodingResponse()

    def test_read_timeout_becomes_an_AlertError(self):
        client = alerts.GitHubClient(
            token="x", repository="a/b", opener=self._ExplodingOpener()
        )
        with self.assertRaises(alerts.AlertError) as caught:
            client._request("GET", "/repos/a/b/labels")
        self.assertIn("transport error", str(caught.exception))

    def test_connection_reset_becomes_an_AlertError(self):
        class ResettingOpener:
            def open(self, request, timeout=None):
                raise ConnectionResetError(104, "connection reset by peer")

        client = alerts.GitHubClient(token="x", repository="a/b", opener=ResettingOpener())
        with self.assertRaises(alerts.AlertError):
            client._request("GET", "/repos/a/b/labels")


class TestAssigneeRetryAtClientLevel(unittest.TestCase):
    """Proves GitHubClient.create_issue itself retries without the assignee.

    Tested against a fake OPENER rather than a fake client, because the retry
    lives inside the real client - a fake client would replace the very code
    under test.
    """

    class _RejectingOpener:
        """422s any create that carries assignees; accepts one that does not."""

        def __init__(self):
            self.payloads = []

        def open(self, request, timeout=None):
            payload = json.loads(request.data.decode("utf-8"))
            self.payloads.append(payload)
            if "assignees" in payload:
                raise fakes.http_error(
                    request.full_url, 422, {}, b'{"message":"Validation Failed"}'
                )
            return fakes.FakeResponse(
                201,
                {"Content-Type": "application/json"},
                json.dumps({"number": 42, "assignees": []}).encode("utf-8"),
            )

    def test_create_issue_retries_without_the_assignee(self):
        opener = self._RejectingOpener()
        client = alerts.GitHubClient(token="x", repository="a/b", opener=opener)

        issue = client.create_issue("t", "b", ["uptime-alert"], ["some-org"])

        self.assertEqual(issue["number"], 42, "the incident must still be created")
        self.assertEqual(len(opener.payloads), 2, "one attempt with, one without")
        self.assertIn("assignees", opener.payloads[0])
        self.assertNotIn("assignees", opener.payloads[1])

    def test_a_failure_with_no_assignee_is_not_swallowed(self):
        class AlwaysFails:
            def open(self, request, timeout=None):
                raise fakes.http_error(request.full_url, 500, {}, b"boom")

        client = alerts.GitHubClient(token="x", repository="a/b", opener=AlwaysFails())
        with self.assertRaises(alerts.AlertError):
            client.create_issue("t", "b", ["uptime-alert"], [])


class TestClientGuards(unittest.TestCase):
    def test_missing_token_is_refused(self):
        with self.assertRaises(alerts.AlertError):
            alerts.GitHubClient(token="", repository="a/b")

    def test_missing_repository_is_refused(self):
        with self.assertRaises(alerts.AlertError):
            alerts.GitHubClient(token="x", repository="")


if __name__ == "__main__":
    unittest.main()
