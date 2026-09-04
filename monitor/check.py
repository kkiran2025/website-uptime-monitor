"""The HTTP layer: one read-only GET per site, with a precise failure taxonomy.

Safety properties, deliberately enforced here rather than by convention:

  * GET only. The method is hard-coded; nothing in this project can POST.
  * No cookies. No cookie jar is installed, so nothing is ever stored or sent.
  * No authentication. No credential is read from the environment or disk.
  * Redirects are followed manually and capped, so a loop is detected rather
    than endured, and an HTTPS-to-HTTP downgrade is refused.
  * Bodies are read under a hard byte cap and a wall-clock deadline.
"""

import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import reasons
from .config import (
    CONNECT_TIMEOUT_SECONDS,
    MAX_ATTEMPTS,
    MAX_BODY_BYTES,
    MAX_REDIRECTS,
    REQUEST_HEADERS,
    RETRY_BACKOFF_SECONDS,
    TOTAL_TIMEOUT_SECONDS,
)
from .validate import validate_body

REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})

#: The only schemes a redirect may ever land on. Anything else - file:, ftp:,
#: data: - is refused outright, never fetched.
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Body is read in chunks this size so the deadline can be re-checked between
#: them. See _read_bounded for why a single read() is not interruptible.
READ_CHUNK_BYTES = 64 * 1024


@dataclass
class CheckOutcome:
    """The full result of checking one site, ready for reporting."""

    site_key: str
    site_name: str
    url: str
    ok: bool
    reason: str = reasons.OK
    detail: str = ""
    http_status: int = None
    final_url: str = ""
    redirects: int = 0
    body_bytes: int = 0
    title: str = ""
    score: int = 0
    total_groups: int = 0
    elapsed_ms: int = 0
    attempts: int = 1
    transient: bool = False

    @property
    def reason_text(self) -> str:
        return reasons.describe(self.reason)


class _Failure(Exception):
    """Internal control-flow: a check failed for a specific, known reason."""

    def __init__(self, reason, detail, http_status=None, final_url="", redirects=0, transient=False):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.http_status = http_status
        self.final_url = final_url
        self.redirects = redirects
        self.transient = transient


@dataclass
class _Fetched:
    status: int
    final_url: str
    body: str
    content_type: str
    redirects: int
    body_bytes: int


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Turn every 3xx into an HTTPError so we can follow redirects ourselves."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def build_opener():
    """A minimal opener: TLS verification on, no cookies, no redirects, no auth.

    Built from an EXPLICIT handler list rather than urllib.request.build_opener,
    because build_opener always includes FileHandler, FTPHandler and DataHandler.
    With those present, a redirect to `file:///etc/passwd`, `ftp://...` or a
    `data:` URL would actually be fetched - which would break the "public HTTP
    GET only" guarantee this monitor is built on. Verified: before this change,
    a `Location: file:///etc/hostname` redirect really did read the local file.

    This is the first of two layers. The second is the scheme allowlist applied
    to every redirect target in _fetch.
    """
    context = ssl.create_default_context()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED

    opener = urllib.request.OpenerDirector()
    for handler in (
        # ProxyHandler() with no argument calls getproxies(), which reads
        # HTTP_PROXY/HTTPS_PROXY - and a proxy URL containing userinfo would
        # then attach a Proxy-Authorization header to the site check. That
        # would break the "no credentials are ever sent" guarantee. An empty
        # mapping disables proxy support entirely: these are direct public GETs.
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
        urllib.request.HTTPHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
        _NoRedirectHandler(),
        # Raises URLError for any scheme we have no handler for, which is
        # exactly what we want for file:, ftp:, data: and friends.
        urllib.request.UnknownHandler(),
    ):
        opener.add_handler(handler)
    return opener


def _normalise(url: str) -> str:
    """Canonical form used for redirect-loop detection (fragment is irrelevant)."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, "")
    )


def _classify_exception(exc, final_url, redirects):
    """Map a transport-layer exception onto one taxonomy code.

    Order matters: ssl.SSLError, socket.gaierror and TimeoutError are all
    subclasses of OSError, so the specific cases must be tested first.
    """
    reason = getattr(exc, "reason", None)
    inner = reason if isinstance(reason, BaseException) else exc

    # ssl.SSLCertVerificationError subclasses ssl.SSLError, so this one branch
    # covers both expired/invalid certificates and handshake failures.
    if isinstance(inner, ssl.SSLError):
        code, transient = reasons.TLS_FAILURE, False
    elif isinstance(inner, socket.gaierror):
        code, transient = reasons.DNS_FAILURE, True
    elif isinstance(inner, (socket.timeout, TimeoutError)):
        code, transient = reasons.TIMEOUT, True
    elif isinstance(inner, ConnectionError):
        code, transient = reasons.CONNECTION_FAILURE, True
    elif isinstance(inner, OSError):
        code, transient = reasons.CONNECTION_FAILURE, True
    else:
        code, transient = reasons.CONNECTION_FAILURE, True

    return _Failure(
        code,
        f"{type(inner).__name__}: {inner}",
        final_url=final_url,
        redirects=redirects,
        transient=transient,
    )


def _decode(raw: bytes, content_type: str) -> str:
    charset = "utf-8"
    if content_type and "charset=" in content_type.lower():
        charset = content_type.lower().split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except (LookupError, TypeError):
        return raw.decode("utf-8", errors="replace")


def _fetch(url, opener, deadline):
    """Follow redirects manually and return the final response, or raise _Failure."""
    current = url
    visited = [_normalise(url)]
    redirects = 0

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _Failure(
                reasons.TIMEOUT,
                f"exceeded the {TOTAL_TIMEOUT_SECONDS:.0f}s budget after {redirects} redirect(s)",
                final_url=current,
                redirects=redirects,
                transient=True,
            )

        timeout = min(CONNECT_TIMEOUT_SECONDS, remaining)
        request = urllib.request.Request(current, headers=dict(REQUEST_HEADERS), method="GET")

        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in REDIRECT_CODES:
                location = exc.headers.get("Location") if exc.headers else None
                try:
                    exc.close()
                except Exception:  # pragma: no cover - defensive
                    pass
                if not location:
                    raise _Failure(
                        reasons.REDIRECT_PROBLEM,
                        f"HTTP {exc.code} with no Location header",
                        http_status=exc.code,
                        final_url=current,
                        redirects=redirects,
                    )
                target = urllib.parse.urljoin(current, location)
                target_scheme = urllib.parse.urlsplit(target).scheme.lower()
                if target_scheme not in ALLOWED_SCHEMES:
                    raise _Failure(
                        reasons.REDIRECT_PROBLEM,
                        f"redirect to a non-HTTP scheme ({target_scheme or 'relative'})",
                        http_status=exc.code,
                        final_url=current,
                        redirects=redirects,
                    )
                if urllib.parse.urlsplit(current).scheme == "https" and target_scheme == "http":
                    raise _Failure(
                        reasons.REDIRECT_PROBLEM,
                        "redirect downgraded HTTPS to HTTP",
                        http_status=exc.code,
                        final_url=target,
                        redirects=redirects,
                    )
                if _normalise(target) in visited:
                    raise _Failure(
                        reasons.REDIRECT_LOOP,
                        f"redirected back to an already-visited URL after {redirects + 1} hop(s)",
                        http_status=exc.code,
                        final_url=target,
                        redirects=redirects + 1,
                    )
                if redirects + 1 > MAX_REDIRECTS:
                    raise _Failure(
                        reasons.REDIRECT_PROBLEM,
                        f"more than {MAX_REDIRECTS} redirect hops",
                        http_status=exc.code,
                        final_url=target,
                        redirects=redirects + 1,
                    )
                visited.append(_normalise(target))
                current = target
                redirects += 1
                continue

            # A non-redirect error status is still a real response.
            try:
                exc.close()
            except Exception:  # pragma: no cover - defensive
                pass
            raise _Failure(
                reasons.HTTP_STATUS,
                f"final response was HTTP {exc.code}",
                http_status=exc.code,
                final_url=current,
                redirects=redirects,
                transient=500 <= exc.code <= 599,
            )
        except urllib.error.URLError as exc:
            raise _classify_exception(exc, current, redirects) from exc
        except (ssl.SSLError, socket.timeout, TimeoutError, OSError) as exc:
            raise _classify_exception(exc, current, redirects) from exc

        try:
            with response:
                status = getattr(response, "status", None) or response.getcode()
                content_type = response.headers.get("Content-Type", "") if response.headers else ""
                raw = _read_bounded(response, deadline, current, redirects, status)
        except (socket.timeout, TimeoutError) as exc:
            raise _classify_exception(exc, current, redirects) from exc
        except OSError as exc:
            raise _classify_exception(exc, current, redirects) from exc

        if len(raw) > MAX_BODY_BYTES:
            raw = raw[:MAX_BODY_BYTES]

        # A socket timeout in Python bounds each blocking recv, NOT the whole
        # read. A body arriving in slow dribbles can therefore overrun the
        # budget even though every individual chunk was inside the timeout.
        # Enforce the total budget explicitly so "completes within 30 seconds"
        # is a real guarantee rather than an approximate one.
        if time.monotonic() > deadline:
            raise _Failure(
                reasons.TIMEOUT,
                f"response body did not finish within the {TOTAL_TIMEOUT_SECONDS:.0f}s budget",
                http_status=status,
                final_url=current,
                redirects=redirects,
                transient=True,
            )

        if status != 200:
            raise _Failure(
                reasons.HTTP_STATUS,
                f"final response was HTTP {status}",
                http_status=status,
                final_url=current,
                redirects=redirects,
                transient=500 <= int(status) <= 599,
            )

        return _Fetched(
            status=status,
            final_url=current,
            body=_decode(raw, content_type),
            content_type=content_type,
            redirects=redirects,
            body_bytes=len(raw),
        )



def _read_bounded(response, deadline, current, redirects, status=None):
    """Read the body in chunks, re-checking the wall-clock deadline between them.

    A single ``response.read()`` cannot be interrupted: a Python socket timeout
    bounds each individual ``recv``, not the whole read. An origin that dribbles
    bytes slightly faster than the timeout therefore blocks until EOF or the
    byte cap - potentially for hours.

    That is not a hypothetical. It is the classic shape of a *dying* server:
    the connection is accepted, then data trickles. Under the previous code the
    Actions job would hit its own timeout and be killed BEFORE recording an
    alert, so a slow-death outage produced silence rather than a notification.

    Checking the deadline between chunks bounds the total wait to the budget
    plus at most one socket timeout for the chunk in flight.

    The chunk MUST be fetched with ``read1``, not ``read``. ``read(n)`` keeps
    blocking until it has accumulated n bytes or hit EOF, so a large chunk size
    bounds nothing at all - measured against a trickling local server,
    ``read(65536)`` blocked for 8.2s while ``read1(65536)`` returned 8 bytes
    immediately. Only ``read1`` performs at most one underlying socket read and
    hands control back so the deadline can be re-checked.
    """
    declared = None
    if response.headers is not None:
        try:
            declared = int(response.headers.get("Content-Length"))
        except (TypeError, ValueError):
            declared = None

    chunks = []
    total = 0
    while total <= MAX_BODY_BYTES:
        if time.monotonic() > deadline:
            raise _Failure(
                reasons.TIMEOUT,
                f"response body did not finish within the {TOTAL_TIMEOUT_SECONDS:.0f}s budget",
                http_status=status,
                final_url=current,
                redirects=redirects,
                transient=True,
            )
        # read1 => at most one underlying socket read, so control returns here
        # and the deadline above is actually enforced. Falls back to read only
        # for objects that do not implement read1.
        reader = getattr(response, "read1", None) or response.read
        chunk = reader(READ_CHUNK_BYTES)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)

    hit_cap = total > MAX_BODY_BYTES

    # http.client.HTTPResponse.read(amt) does NOT raise IncompleteRead when the
    # origin closes early - CPython's own source says so: "Ideally, we would
    # raise IncompleteRead if the content-length wasn't satisfied, but it might
    # break compatibility." It returns the partial bytes and then b'', which is
    # indistinguishable from a clean EOF. Verified against a local socket:
    # Content-Length 1000, 39 bytes delivered, no exception.
    #
    # That matters because a homepage truncated mid-render whose FIRST chunk
    # already carries the storefront markers would otherwise be scored healthy.
    # Compare the declared length against what actually arrived.
    if declared is not None and not hit_cap and total < declared:
        raise _Failure(
            reasons.TRUNCATED_RESPONSE,
            f"received {total} of {declared} declared bytes before the connection closed",
            http_status=status,
            final_url=current,
            redirects=redirects,
            transient=True,
        )

    return b"".join(chunks)[: MAX_BODY_BYTES + 1]


def _single_attempt(site, opener) -> CheckOutcome:
    started = time.monotonic()
    deadline = started + TOTAL_TIMEOUT_SECONDS
    base = dict(site_key=site.key, site_name=site.name, url=site.url)

    try:
        fetched = _fetch(site.url, opener, deadline)
    except _Failure as failure:
        return CheckOutcome(
            **base,
            ok=False,
            reason=failure.reason,
            detail=failure.detail,
            http_status=failure.http_status,
            final_url=failure.final_url or site.url,
            redirects=failure.redirects,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            transient=failure.transient,
        )

    result = validate_body(fetched.body, site.brand_patterns, fetched.content_type)
    return CheckOutcome(
        **base,
        ok=result.ok,
        reason=result.reason,
        detail=result.detail,
        http_status=fetched.status,
        final_url=fetched.final_url,
        redirects=fetched.redirects,
        body_bytes=fetched.body_bytes,
        title=result.title,
        score=result.score,
        total_groups=result.total_groups,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        transient=False,
    )


def check_site(site, opener=None, sleep=time.sleep) -> CheckOutcome:
    """Check one site, retrying only genuinely transient transport failures."""
    opener = opener or build_opener()
    outcome = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        outcome = _single_attempt(site, opener)
        outcome.attempts = attempt
        if outcome.ok or not outcome.transient:
            return outcome
        if attempt < MAX_ATTEMPTS:
            sleep(RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)])

    return outcome


def check_all(sites, opener=None, sleep=time.sleep):
    """Check every site. One site failing never prevents the next being checked."""
    opener = opener or build_opener()
    results = []
    for site in sites:
        try:
            results.append(check_site(site, opener=opener, sleep=sleep))
        except Exception as exc:  # pragma: no cover - last-resort guard
            results.append(
                CheckOutcome(
                    site_key=site.key,
                    site_name=site.name,
                    url=site.url,
                    ok=False,
                    reason=reasons.CONNECTION_FAILURE,
                    detail=f"unexpected monitor error: {type(exc).__name__}",
                    final_url=site.url,
                )
            )
    return results
