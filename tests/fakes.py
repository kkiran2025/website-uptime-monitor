"""Test doubles: a scripted HTTP opener and an in-memory GitHub client.

Everything here is offline. No test in this suite opens a socket, so failure
handling is proven without touching either real website.
"""

import email.message
import io
import urllib.error


def make_headers(mapping):
    message = email.message.Message()
    for key, value in (mapping or {}).items():
        message[key] = value
    return message


class FakeResponse:
    """Models a real HTTP response stream: read() advances and then returns b"".

    Getting this wrong hides bugs. An earlier version returned the whole body on
    every call, so a chunked reader looped until it hit the byte cap instead of
    ever reaching EOF - which masked the truncation check under test.
    """

    def __init__(self, status=200, headers=None, body=b""):
        self.status = status
        self.headers = make_headers(headers or {"Content-Type": "text/html; charset=UTF-8"})
        self._body = body if isinstance(body, bytes) else body.encode("utf-8")
        self._offset = 0

    def read(self, amount=None):
        if amount is None:
            chunk = self._body[self._offset:]
            self._offset = len(self._body)
            return chunk
        chunk = self._body[self._offset:self._offset + amount]
        self._offset += len(chunk)
        return chunk

    # http.client.HTTPResponse implements read1; the production code prefers it
    # because read() blocks until the buffer is full. The fake must expose it
    # too, or the tests would exercise a path the real code never takes.
    def read1(self, amount=None):
        return self.read(amount)

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


def http_error(url, code, headers=None, body=b""):
    return urllib.error.HTTPError(url, code, f"HTTP {code}", make_headers(headers or {}), io.BytesIO(body))


def redirect(url, code=302, location="/"):
    return http_error(url, code, {"Location": location})


class FakeOpener:
    """Drives responses from a callable, a per-URL map, or a fixed sequence."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    def open(self, request, timeout=None):
        url = request.full_url
        self.calls.append((url, request.get_method(), dict(request.header_items()), timeout))

        if callable(self.script):
            outcome = self.script(url, len(self.calls) - 1)
        elif isinstance(self.script, dict):
            outcome = self.script[url]
        else:
            index = min(len(self.calls) - 1, len(self.script) - 1)
            outcome = self.script[index]

        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeGitHubClient:
    """In-memory stand-in for alerts.GitHubClient. Records every mutation."""

    def __init__(self, open_issue=None, assignable=True, reject_assignees=False):
        self.open_issue = open_issue
        self.assignable = assignable
        #: Simulate GitHub's 422 when the owner is not an assignable user.
        self.reject_assignees = reject_assignees
        self.created = []
        self.comments = []
        self.updates = []
        self.closed = []
        self.labels_ensured = 0

    def existing_labels(self):
        return set()

    def ensure_labels(self):
        self.labels_ensured += 1
        return []

    def is_assignable(self, login):
        return self.assignable

    def find_open_incident(self, site_label):
        return self.open_issue

    def create_issue(self, title, body, labels, assignees):
        if self.reject_assignees:
            # Mirror what the REAL client does after GitHub 422s a bad
            # assignee: it retries without one, so the issue still exists.
            # The retry itself is covered at the client level in test_alerts.
            assignees = []
        issue = {
            "number": 1,
            "title": title,
            "body": body,
            "labels": list(labels),
            # The real API returns USER OBJECTS here, not bare strings. Modelling
            # that correctly matters - an earlier version returned strings and
            # hid an AttributeError in the production path.
            "assignees": [{"login": a} for a in assignees],
            "created_at": "2026-09-04T10:00:00Z",
        }
        self.created.append(issue)
        return issue

    def add_comment(self, number, body):
        self.comments.append((number, body))
        return 201, {}

    def update_issue(self, number, payload):
        self.updates.append((number, payload))
        return 200, {}

    def close_issue(self, number):
        self.closed.append(number)
        return self.update_issue(number, {"state": "closed"})
