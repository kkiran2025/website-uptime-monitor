"""The failure taxonomy.

Every failed check resolves to exactly one of these codes. Keeping them in one
place means the alerting layer, the job summary and the tests all agree on the
vocabulary, and it lets us say precisely *why* a check failed rather than
"site down".
"""

OK = "ok"

# --- Network / transport layer ---------------------------------------------
DNS_FAILURE = "dns_failure"
TLS_FAILURE = "tls_failure"
CONNECTION_FAILURE = "connection_failure"
TIMEOUT = "timeout"

# --- HTTP layer -------------------------------------------------------------
REDIRECT_LOOP = "redirect_loop"
REDIRECT_PROBLEM = "redirect_problem"
HTTP_STATUS = "http_status"

# --- Body layer -------------------------------------------------------------
EMPTY_RESPONSE = "empty_response"
TRUNCATED_RESPONSE = "truncated_response"
NOT_HTML = "not_html"
ERROR_PAGE = "error_page"
MISSING_STOREFRONT_INDICATORS = "missing_storefront_indicators"

#: Human-readable one-liners, used in issue bodies and the job summary.
DESCRIPTIONS = {
    OK: "Healthy",
    DNS_FAILURE: "DNS lookup failed - the hostname did not resolve",
    TLS_FAILURE: "TLS/SSL handshake or certificate validation failed",
    CONNECTION_FAILURE: "TCP connection to the server failed",
    TIMEOUT: "The request did not complete within the time budget",
    REDIRECT_LOOP: "The server redirected back to a URL already visited",
    REDIRECT_PROBLEM: "Too many redirect hops, a missing Location header, or an HTTPS-to-HTTP downgrade",
    HTTP_STATUS: "The final response was not HTTP 200",
    EMPTY_RESPONSE: "The response body was empty or implausibly small",
    TRUNCATED_RESPONSE: "The server closed the connection before sending the whole page",
    NOT_HTML: "The response was not HTML",
    ERROR_PAGE: "The page rendered a server, maintenance, firewall, database, PHP or WordPress error",
    MISSING_STOREFRONT_INDICATORS: "The page loaded but expected storefront content was absent",
}

#: Classes worth retrying: a transient blip, not a real fault. Content-level
#: failures are deliberately absent - retrying those would hide a genuine
#: outage behind three identical attempts.
TRANSIENT = frozenset({DNS_FAILURE, CONNECTION_FAILURE, TIMEOUT, TRUNCATED_RESPONSE})


def describe(code: str) -> str:
    return DESCRIPTIONS.get(code, code)
