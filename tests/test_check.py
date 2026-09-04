"""HTTP-layer tests. Every failure mode is exercised offline, via fake
responses - neither real website is ever contacted, slowed, or altered."""

import os
import socket
import ssl
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from monitor import check as check_module
from monitor import reasons
from monitor.check import _normalise, check_all, check_site
from monitor.config import MAX_ATTEMPTS, Site
from tests import fakes, fixtures

SITE = Site(
    key="test-site",
    name="Test Site",
    url="https://example.test/",
    label="uptime-test",
    brand_patterns=("direct packaging",),
)

HTML_HEADERS = {"Content-Type": "text/html; charset=UTF-8"}


def ok_response(body=None):
    return fakes.FakeResponse(200, HTML_HEADERS, body or fixtures.HEALTHY_DIRECT_PACKAGING)


def run(site, script, sleeps=None):
    recorded = sleeps if sleeps is not None else []
    opener = fakes.FakeOpener(script)
    outcome = check_site(site, opener=opener, sleep=recorded.append)
    return outcome, opener, recorded


class TestHappyPath(unittest.TestCase):
    def test_healthy_site(self):
        outcome, opener, sleeps = run(SITE, [ok_response()])
        self.assertTrue(outcome.ok, outcome.detail)
        self.assertEqual(outcome.reason, reasons.OK)
        self.assertEqual(outcome.http_status, 200)
        self.assertEqual(outcome.redirects, 0)
        self.assertEqual(outcome.attempts, 1)
        self.assertEqual(sleeps, [])

    def test_only_get_is_ever_issued(self):
        _, opener, _ = run(SITE, [ok_response()])
        methods = {call[1] for call in opener.calls}
        self.assertEqual(methods, {"GET"})

    def test_no_cookie_header_is_sent(self):
        _, opener, _ = run(SITE, [ok_response()])
        headers = {key.lower() for key in opener.calls[0][2]}
        self.assertNotIn("cookie", headers)
        self.assertNotIn("authorization", headers)

    def test_identifiable_user_agent(self):
        _, opener, _ = run(SITE, [ok_response()])
        agent = next(v for k, v in opener.calls[0][2].items() if k.lower() == "user-agent")
        self.assertIn("WebsiteUptimeMonitor", agent)
        self.assertIn("github.com", agent)

    def test_legitimate_redirects_are_followed(self):
        script = [
            fakes.redirect("https://example.test/", 301, "https://www.example.test/"),
            fakes.redirect("https://www.example.test/", 302, "https://www.example.test/shop/"),
            ok_response(),
        ]
        outcome, _, _ = run(SITE, script)
        self.assertTrue(outcome.ok, outcome.detail)
        self.assertEqual(outcome.redirects, 2)
        self.assertEqual(outcome.final_url, "https://www.example.test/shop/")


class TestRedirectProblems(unittest.TestCase):
    def test_redirect_loop(self):
        script = [
            fakes.redirect("https://example.test/", 302, "https://example.test/a"),
            fakes.redirect("https://example.test/a", 302, "https://example.test/"),
        ]
        outcome, _, _ = run(SITE, script)
        self.assertEqual(outcome.reason, reasons.REDIRECT_LOOP)
        self.assertFalse(outcome.transient)

    def test_self_redirect_loop(self):
        script = [fakes.redirect("https://example.test/", 302, "https://example.test/")]
        outcome, _, _ = run(SITE, script)
        self.assertEqual(outcome.reason, reasons.REDIRECT_LOOP)

    def test_too_many_hops(self):
        script = lambda url, i: fakes.redirect(url, 302, f"https://example.test/hop{i}")
        outcome, _, _ = run(SITE, script)
        self.assertEqual(outcome.reason, reasons.REDIRECT_PROBLEM)
        self.assertIn("redirect hops", outcome.detail)

    def test_missing_location_header(self):
        script = [fakes.http_error("https://example.test/", 302, {})]
        outcome, _, _ = run(SITE, script)
        self.assertEqual(outcome.reason, reasons.REDIRECT_PROBLEM)
        self.assertIn("no Location", outcome.detail)

    def test_redirect_to_file_scheme_is_refused(self):
        """Regression guard: a `Location: file://` once really did read a local
        file, because urllib.request.build_opener always installs FileHandler."""
        script = [fakes.redirect("https://example.test/", 302, "file:///etc/hostname")]
        outcome, _, _ = run(SITE, script)
        self.assertEqual(outcome.reason, reasons.REDIRECT_PROBLEM)
        self.assertIn("non-HTTP scheme", outcome.detail)

    def test_redirect_to_data_scheme_is_refused(self):
        script = [fakes.redirect("https://example.test/", 302, "data:text/html,<html>hi</html>")]
        outcome, _, _ = run(SITE, script)
        self.assertEqual(outcome.reason, reasons.REDIRECT_PROBLEM)

    def test_redirect_to_ftp_scheme_is_refused(self):
        script = [fakes.redirect("https://example.test/", 302, "ftp://example.test/x")]
        outcome, _, _ = run(SITE, script)
        self.assertEqual(outcome.reason, reasons.REDIRECT_PROBLEM)

    def test_https_to_http_downgrade_is_refused(self):
        script = [fakes.redirect("https://example.test/", 301, "http://example.test/")]
        outcome, _, _ = run(SITE, script)
        self.assertEqual(outcome.reason, reasons.REDIRECT_PROBLEM)
        self.assertIn("downgraded", outcome.detail)


class TestTransportFailures(unittest.TestCase):
    def test_dns_failure(self):
        err = urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))
        outcome, _, sleeps = run(SITE, [err])
        self.assertEqual(outcome.reason, reasons.DNS_FAILURE)
        self.assertEqual(outcome.attempts, MAX_ATTEMPTS)
        self.assertEqual(len(sleeps), MAX_ATTEMPTS - 1)

    def test_tls_failure(self):
        err = urllib.error.URLError(ssl.SSLCertVerificationError("certificate has expired"))
        outcome, _, sleeps = run(SITE, [err])
        self.assertEqual(outcome.reason, reasons.TLS_FAILURE)
        self.assertEqual(outcome.attempts, 1, "a bad certificate is not transient")
        self.assertEqual(sleeps, [])

    def test_connection_refused(self):
        outcome, _, _ = run(SITE, [urllib.error.URLError(ConnectionRefusedError(111, "refused"))])
        self.assertEqual(outcome.reason, reasons.CONNECTION_FAILURE)

    def test_timeout(self):
        outcome, _, _ = run(SITE, [urllib.error.URLError(socket.timeout("timed out"))])
        self.assertEqual(outcome.reason, reasons.TIMEOUT)
        self.assertEqual(outcome.attempts, MAX_ATTEMPTS)

    def test_transient_failure_that_recovers_on_retry(self):
        script = [urllib.error.URLError(socket.timeout("timed out")), ok_response()]
        outcome, opener, sleeps = run(SITE, script)
        self.assertTrue(outcome.ok, outcome.detail)
        self.assertEqual(outcome.attempts, 2)
        self.assertEqual(len(sleeps), 1)


class TestTimeoutBudget(unittest.TestCase):
    """The 30s budget must bound the body read, not just the connect."""

    def test_slow_body_read_overruns_the_budget_and_is_reported_as_a_timeout(self):
        class SlowResponse(fakes.FakeResponse):
            def read(self, amount=None):
                # A body that dribbles in: every individual recv is inside the
                # socket timeout, but the whole read overruns the total budget.
                time.sleep(0.08)
                return super().read(amount)

        # A fresh response per attempt, exactly as a real retry would get.
        def fresh(_url, _index):
            return SlowResponse(200, HTML_HEADERS, fixtures.HEALTHY_DIRECT_PACKAGING)

        with mock.patch.object(check_module, "TOTAL_TIMEOUT_SECONDS", 0.02):
            outcome, _, _ = run(SITE, fresh)

        self.assertEqual(outcome.reason, reasons.TIMEOUT)
        self.assertIn("body did not finish", outcome.detail)
        self.assertTrue(outcome.transient, "a slow body is transient and should be retried")

    def test_a_dribbling_body_is_cut_off_at_the_deadline(self):
        """The slow-death outage shape: bytes trickle in, never EOF.

        A single response.read() cannot be interrupted, so before the body was
        read in chunks this blocked until the Actions job was killed - and the
        outage produced NO alert at all.
        """

        class DribblingResponse(fakes.FakeResponse):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.reads = 0

            def read(self, amount=None):
                self.reads += 1
                time.sleep(0.005)
                return b"x" * 8  # never returns b"" — i.e. never reaches EOF

        dribbler = DribblingResponse(200, HTML_HEADERS, "")
        with mock.patch.object(check_module, "TOTAL_TIMEOUT_SECONDS", 0.05):
            outcome, _, _ = run(SITE, [dribbler, dribbler, dribbler])

        self.assertEqual(outcome.reason, reasons.TIMEOUT)
        self.assertTrue(outcome.transient)
        self.assertLess(
            dribbler.reads, 500,
            "the read loop must be bounded by the deadline, not run until EOF",
        )

    def test_a_fast_response_is_not_falsely_timed_out(self):
        outcome, _, _ = run(SITE, [ok_response()])
        self.assertTrue(outcome.ok, outcome.detail)
        self.assertNotEqual(outcome.reason, reasons.TIMEOUT)


class TestTruncatedBodies(unittest.TestCase):
    """A body cut short mid-flight must never be scored as a healthy page."""

    def test_short_body_against_declared_content_length_is_truncated(self):
        body = fixtures.HEALTHY_DIRECT_PACKAGING
        headers = {"Content-Type": "text/html", "Content-Length": str(len(body) * 4)}
        outcome, _, _ = run(SITE, [fakes.FakeResponse(200, headers, body)])
        self.assertEqual(outcome.reason, reasons.TRUNCATED_RESPONSE)
        self.assertTrue(outcome.transient, "a dropped connection is worth retrying")
        self.assertIn("declared bytes", outcome.detail)

    def test_truncation_preserves_the_known_http_status(self):
        """A truncated 200 must not be reported as 'no response'."""
        body = fixtures.HEALTHY_DIRECT_PACKAGING
        headers = {"Content-Type": "text/html", "Content-Length": str(len(body) * 4)}
        outcome, _, _ = run(SITE, [fakes.FakeResponse(200, headers, body)])
        self.assertEqual(outcome.reason, reasons.TRUNCATED_RESPONSE)
        self.assertEqual(outcome.http_status, 200, "the status was known; do not drop it")

    def test_matching_content_length_passes(self):
        body = fixtures.HEALTHY_DIRECT_PACKAGING
        headers = {
            "Content-Type": "text/html",
            "Content-Length": str(len(body.encode("utf-8"))),
        }
        outcome, _, _ = run(SITE, [fakes.FakeResponse(200, headers, body)])
        self.assertTrue(outcome.ok, outcome.detail)

    def test_absent_content_length_is_not_treated_as_truncated(self):
        """Chunked transfer-encoding sends no Content-Length; that is normal."""
        outcome, _, _ = run(SITE, [ok_response()])
        self.assertTrue(outcome.ok, outcome.detail)

    def test_hitting_the_byte_cap_is_not_truncation(self):
        big = fixtures.HEALTHY_DIRECT_PACKAGING + ("<p>padding</p>" * 200)
        headers = {"Content-Type": "text/html", "Content-Length": "999999999"}
        with mock.patch.object(check_module, "MAX_BODY_BYTES", 128), mock.patch.object(
            check_module, "READ_CHUNK_BYTES", 64
        ):
            outcome, _, _ = run(SITE, [fakes.FakeResponse(200, headers, big)])
        self.assertNotEqual(
            outcome.reason, reasons.TRUNCATED_RESPONSE,
            "stopping at our own byte cap is not the server truncating us",
        )


class TestAgainstARealTruncatingServer(unittest.TestCase):
    """End-to-end over a real socket on localhost. No external network."""

    def _serve(self, declared_multiplier):
        body = fixtures.HEALTHY_DIRECT_PACKAGING.encode("utf-8")
        declared = int(len(body) * declared_multiplier)
        header = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/html; charset=UTF-8\r\n"
            "Connection: close\r\n"
            f"Content-Length: {declared}\r\n"
            "\r\n"
        ).encode("ascii")

        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        port = server.getsockname()[1]

        def handle():
            try:
                for _ in range(check_module.MAX_ATTEMPTS):
                    conn, _addr = server.accept()
                    conn.recv(65535)
                    conn.sendall(header)
                    conn.sendall(body)
                    conn.close()
            except OSError:
                pass
            finally:
                server.close()

        threading.Thread(target=handle, daemon=True).start()
        return port

    def _site(self, port):
        return Site("local", "Local", f"http://127.0.0.1:{port}/", "uptime-local", ("direct packaging",))

    def test_a_real_truncated_response_is_caught(self):
        """The server promises 5x the bytes it actually sends, then closes."""
        port = self._serve(declared_multiplier=5)
        outcome = check_site(self._site(port), sleep=lambda _s: None)
        self.assertEqual(outcome.reason, reasons.TRUNCATED_RESPONSE, outcome.detail)

    def test_a_real_complete_response_passes(self):
        port = self._serve(declared_multiplier=1)
        outcome = check_site(self._site(port), sleep=lambda _s: None)
        self.assertTrue(outcome.ok, f"{outcome.reason}: {outcome.detail}")


class TestAgainstARealDribblingServer(unittest.TestCase):
    """The definitive guard for the slow-death outage shape.

    A mock cannot catch this: only a real socket reveals that read(n) blocks
    until the buffer fills. Measured, read(65536) blocked 8.2s on a trickling
    server while read1(65536) returned immediately.
    """

    def _serve_forever_slowly(self):
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        port = server.getsockname()[1]
        stop = threading.Event()

        def handle():
            try:
                while not stop.is_set():
                    server.settimeout(1.0)
                    try:
                        conn, _addr = server.accept()
                    except socket.timeout:
                        continue
                    conn.recv(65535)
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n")
                    try:
                        while not stop.is_set():
                            conn.sendall(b"x" * 8)   # trickle, never finish
                            time.sleep(0.02)
                    except OSError:
                        pass
                    finally:
                        conn.close()
            except OSError:
                pass
            finally:
                server.close()

        threading.Thread(target=handle, daemon=True).start()
        self.addCleanup(stop.set)
        return port

    def test_deadline_is_honoured_against_a_trickling_origin(self):
        port = self._serve_forever_slowly()
        site = Site("local", "Local", f"http://127.0.0.1:{port}/", "uptime-local", ("direct packaging",))

        started = time.monotonic()
        with mock.patch.object(check_module, "TOTAL_TIMEOUT_SECONDS", 0.4):
            outcome = check_site(site, sleep=lambda _s: None)
        elapsed = time.monotonic() - started

        self.assertEqual(outcome.reason, reasons.TIMEOUT, outcome.detail)
        self.assertLess(
            elapsed, 10.0,
            "the read loop must return near the deadline; with read() instead of "
            "read1() this blocks until the buffer fills and the job gets killed",
        )


class TestHttpStatus(unittest.TestCase):
    def test_404_is_not_retried(self):
        outcome, _, sleeps = run(SITE, [fakes.http_error("https://example.test/", 404, HTML_HEADERS)])
        self.assertEqual(outcome.reason, reasons.HTTP_STATUS)
        self.assertEqual(outcome.http_status, 404)
        self.assertEqual(outcome.attempts, 1)
        self.assertEqual(sleeps, [])

    def test_500_is_retried(self):
        outcome, _, sleeps = run(SITE, [fakes.http_error("https://example.test/", 500, HTML_HEADERS)])
        self.assertEqual(outcome.reason, reasons.HTTP_STATUS)
        self.assertEqual(outcome.attempts, MAX_ATTEMPTS)
        self.assertEqual(len(sleeps), MAX_ATTEMPTS - 1)

    def test_503_recovers_on_retry(self):
        script = [fakes.http_error("https://example.test/", 503, HTML_HEADERS), ok_response()]
        outcome, _, _ = run(SITE, script)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.attempts, 2)


class TestBodyFailures(unittest.TestCase):
    def test_error_page_behind_a_200(self):
        outcome, _, sleeps = run(SITE, [fakes.FakeResponse(200, HTML_HEADERS, fixtures.APACHE_503)])
        self.assertEqual(outcome.reason, reasons.ERROR_PAGE)
        self.assertEqual(outcome.attempts, 1, "content failures must never be retried")

    def test_empty_body(self):
        outcome, _, _ = run(SITE, [fakes.FakeResponse(200, HTML_HEADERS, "")])
        self.assertEqual(outcome.reason, reasons.EMPTY_RESPONSE)

    def test_not_html(self):
        headers = {"Content-Type": "application/json"}
        outcome, _, _ = run(SITE, [fakes.FakeResponse(200, headers, fixtures.JSON_BODY)])
        self.assertEqual(outcome.reason, reasons.NOT_HTML)

    def test_missing_storefront_indicators(self):
        outcome, _, _ = run(SITE, [fakes.FakeResponse(200, HTML_HEADERS, fixtures.BRAND_ONLY_NO_SHOP)])
        self.assertEqual(outcome.reason, reasons.MISSING_STOREFRONT_INDICATORS)


class TestCheckAll(unittest.TestCase):
    def test_second_site_is_checked_even_when_the_first_fails(self):
        site_a = Site("a", "Site A", "https://a.test/", "uptime-a", ("direct packaging",))
        site_b = Site("b", "Site B", "https://b.test/", "uptime-b", ("direct packaging",))

        def script(url, _index):
            if url.startswith("https://a.test"):
                return urllib.error.URLError(socket.gaierror(-2, "no such host"))
            return ok_response()

        results = check_all((site_a, site_b), opener=fakes.FakeOpener(script), sleep=lambda _s: None)
        self.assertEqual(len(results), 2)
        self.assertFalse(results[0].ok)
        self.assertTrue(results[1].ok, results[1].detail)


class TestOpenerHardening(unittest.TestCase):
    """The opener must be incapable of speaking anything but HTTP(S)."""

    def test_no_file_ftp_or_data_handlers_are_installed(self):
        installed = {type(h).__name__ for h in check_module.build_opener().handlers}
        for forbidden in ("FileHandler", "FTPHandler", "CacheFTPHandler", "DataHandler"):
            with self.subTest(handler=forbidden):
                self.assertNotIn(forbidden, installed)

    def test_http_and_https_are_still_supported(self):
        installed = {type(h).__name__ for h in check_module.build_opener().handlers}
        self.assertIn("HTTPSHandler", installed)
        self.assertIn("HTTPHandler", installed)

    def test_proxy_environment_variables_are_ignored(self):
        """A proxy URL with userinfo would attach Proxy-Authorization to the
        site check, breaking the no-credentials guarantee."""
        with mock.patch.dict(
            os.environ,
            {"HTTPS_PROXY": "http://user:secret@proxy.example:8080",
             "HTTP_PROXY": "http://user:secret@proxy.example:8080"},
        ):
            opener = check_module.build_opener()
            proxies = [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
            for handler in proxies:
                self.assertEqual(handler.proxies, {}, "proxy config must be empty")
            self.assertFalse(
                any(hasattr(h, "https_open") and isinstance(h, urllib.request.ProxyHandler)
                    for h in opener.handlers),
                "no proxy route may be registered",
            )

    def test_opening_a_file_url_is_refused_outright(self):
        opener = check_module.build_opener()
        request = urllib.request.Request("file:///etc/hostname", method="GET")
        with self.assertRaises(urllib.error.URLError):
            opener.open(request, timeout=5)


class TestNormalise(unittest.TestCase):
    def test_fragment_and_case_are_ignored(self):
        self.assertEqual(_normalise("https://Example.test/Path#frag"), "https://example.test/Path")

    def test_query_is_significant(self):
        self.assertNotEqual(_normalise("https://a.test/?x=1"), _normalise("https://a.test/?x=2"))


if __name__ == "__main__":
    unittest.main()
