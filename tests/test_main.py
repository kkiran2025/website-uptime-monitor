"""CLI-level tests, including the safety guarantee that --dry-run is inert."""

import contextlib
import io
import json
import unittest
from unittest import mock

from monitor import main as main_module
from monitor import reasons
from monitor.check import CheckOutcome
from monitor.config import SITES
from tests.fakes import FakeGitHubClient


def outcome(site, ok=True, reason=reasons.OK, status=200, detail="4/4 indicator groups present"):
    return CheckOutcome(
        site_key=site.key,
        site_name=site.name,
        url=site.url,
        ok=ok,
        reason=reason,
        detail=detail,
        http_status=status,
        final_url=site.url,
        score=4,
        total_groups=4,
        elapsed_ms=120,
    )


class TestDryRunSafety(unittest.TestCase):
    def test_dry_run_never_contacts_github(self):
        results = [outcome(site) for site in SITES]
        with mock.patch.object(main_module, "check_all", return_value=results), mock.patch.object(
            main_module.alerts, "client_from_env",
            side_effect=AssertionError("--dry-run must never build a GitHub client"),
        ), mock.patch.object(
            main_module.alerts, "reconcile",
            side_effect=AssertionError("--dry-run must never reconcile issues"),
        ):
            code = main_module.main(["--dry-run"], env={})
        self.assertEqual(code, main_module.EXIT_OK)

    def test_dry_run_still_reports_a_failure_exit_code(self):
        results = [
            outcome(SITES[0], ok=False, reason=reasons.TIMEOUT, status=None, detail="timed out"),
            outcome(SITES[1]),
        ]
        with mock.patch.object(main_module, "check_all", return_value=results), mock.patch.object(
            main_module.alerts, "client_from_env", side_effect=AssertionError("no GitHub in dry-run")
        ):
            code = main_module.main(["--dry-run"], env={})
        self.assertEqual(code, main_module.EXIT_SITE_DOWN)


class TestReporting(unittest.TestCase):
    def test_summary_covers_every_site(self):
        results = [outcome(site) for site in SITES]
        markdown = main_module.step_summary_markdown(results, [], "https://example/run")
        for site in SITES:
            self.assertIn(site.name, markdown)
        self.assertIn("All monitored websites responded correctly.", markdown)

    def test_summary_lists_failure_detail(self):
        results = [
            outcome(SITES[0], ok=False, reason=reasons.ERROR_PAGE, status=200, detail="Cloudflare firewall block"),
            outcome(SITES[1]),
        ]
        markdown = main_module.step_summary_markdown(results, [], "")
        self.assertIn("### Failure detail", markdown)
        self.assertIn("Cloudflare firewall block", markdown)

    def test_console_line_never_includes_page_markup(self):
        line = main_module.summarise(outcome(SITES[0]))
        for token in ["<html", "<div", "<script", "</"]:
            with self.subTest(token=token):
                self.assertNotIn(token, line)


class TestJsonMode(unittest.TestCase):
    """--json advertises machine-readable output, so stdout must be pure JSON."""

    def test_stdout_is_valid_json_and_nothing_else(self):
        results = [outcome(site) for site in SITES]
        buffer = io.StringIO()
        with mock.patch.object(main_module, "check_all", return_value=results), \
                contextlib.redirect_stdout(buffer), \
                contextlib.redirect_stderr(io.StringIO()):
            code = main_module.main(["--dry-run", "--json"], env={})

        self.assertEqual(code, main_module.EXIT_OK)
        parsed = json.loads(buffer.getvalue())  # raises if anything else leaked
        self.assertEqual(len(parsed), len(SITES))
        self.assertEqual({row["site_key"] for row in parsed}, {s.key for s in SITES})

    def test_human_lines_still_appear_on_stderr_in_json_mode(self):
        results = [outcome(site) for site in SITES]
        err = io.StringIO()
        with mock.patch.object(main_module, "check_all", return_value=results), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            main_module.main(["--dry-run", "--json"], env={})
        self.assertIn("Direct Packaging", err.getvalue())

    def test_without_json_the_human_lines_are_on_stdout(self):
        results = [outcome(site) for site in SITES]
        buffer = io.StringIO()
        with mock.patch.object(main_module, "check_all", return_value=results), \
                contextlib.redirect_stdout(buffer):
            main_module.main(["--dry-run"], env={})
        self.assertIn("Direct Packaging", buffer.getvalue())


class TestNotificationTestMode(unittest.TestCase):
    """Raises a test issue as the bot; must never look like an incident."""

    def _run(self, client, env=None):
        env = env or {"GITHUB_TOKEN": "x", "GITHUB_REPOSITORY": "kkiran2025/repo",
                      "GITHUB_REPOSITORY_OWNER": "kkiran2025"}
        with mock.patch.object(main_module.alerts, "client_from_env", return_value=client), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = main_module.run_notification_test(env)
        return code, out.getvalue()

    def test_creates_one_issue_assigned_to_the_owner(self):
        client = FakeGitHubClient(open_issue=None)
        code, _ = self._run(client)
        self.assertEqual(code, main_module.EXIT_OK)
        self.assertEqual(len(client.created), 1)
        self.assertEqual(
            [a["login"] for a in client.created[0]["assignees"]], ["kkiran2025"]
        )

    def test_the_test_issue_carries_no_uptime_label(self):
        """Otherwise the incident logic would mistake it for a real outage."""
        client = FakeGitHubClient(open_issue=None)
        self._run(client)
        self.assertEqual(client.created[0]["labels"], [])

    def test_body_makes_clear_it_is_not_an_incident(self):
        client = FakeGitHubClient(open_issue=None)
        self._run(client)
        self.assertIn("not an incident", client.created[0]["body"])

    def test_reports_an_error_when_the_owner_cannot_be_resolved(self):
        client = FakeGitHubClient(open_issue=None)
        code, _ = self._run(client, env={"GITHUB_TOKEN": "x", "GITHUB_REPOSITORY": "no-slash"})
        self.assertEqual(code, main_module.EXIT_MONITOR_ERROR)
        self.assertEqual(client.created, [])

    def test_a_normal_run_never_triggers_it(self):
        results = [outcome(site) for site in SITES]
        with mock.patch.object(main_module, "check_all", return_value=results), \
                mock.patch.object(main_module, "run_notification_test",
                                  side_effect=AssertionError("must not fire")), \
                mock.patch.object(main_module.alerts, "client_from_env",
                                  side_effect=AssertionError("no GitHub in dry-run")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main_module.main(["--dry-run"], env={}), main_module.EXIT_OK)


class TestAssignee(unittest.TestCase):
    def test_prefers_the_owner_variable(self):
        self.assertEqual(main_module.resolve_assignee({"GITHUB_REPOSITORY_OWNER": "kkiran2025"}), "kkiran2025")

    def test_falls_back_to_the_repository_slug(self):
        self.assertEqual(main_module.resolve_assignee({"GITHUB_REPOSITORY": "kkiran2025/repo"}), "kkiran2025")

    def test_returns_none_when_unknown(self):
        self.assertIsNone(main_module.resolve_assignee({}))


class TestUnknownSite(unittest.TestCase):
    def test_unknown_site_key_is_an_error(self):
        self.assertEqual(main_module.main(["--dry-run", "--site", "nope"], env={}), main_module.EXIT_MONITOR_ERROR)


if __name__ == "__main__":
    unittest.main()
