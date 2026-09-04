"""Pure content validation - no network, no I/O, fully unit-testable.

Two jobs:

1. Decide whether a 200 response is actually an error page wearing a 200.
2. Decide whether the page still looks like the storefront it is supposed to be.

Both are deliberately built to *avoid false alarms*, because a monitor that
cries wolf gets muted and then the real outage is missed.

The anti-false-alarm design, and why it is shaped this way
---------------------------------------------------------
Scanning a whole page body for strings like "503" or "not found" does not work.
Measured on 2026-09-04 against the two real, perfectly healthy homepages:

    directpackaging.online  contains "503" x2 and "not found" x3
    mrcorn.uk.com           contains "cloudflare" x1

Those live inside inline JavaScript, product data and third-party snippets. A
naive substring rule would therefore have reported both shops as down on day
one. So error detection is constrained three ways:

    * Only the <title> and the first HEAD_SCAN_BYTES of the body are scanned -
      real error pages put their message at the very top; storefront noise
      lives deep in the body.
    * A hit in the <title> is fatal on its own (a healthy shop never titles
      itself "503 Service Unavailable").
    * A hit in the head region is fatal only when the page has already fallen
      below the storefront bar. A page still carrying brand, shop, price and
      basket markup is a working shop, whatever stray substrings it contains -
      so the storefront score, not the page size, is the deciding signal.

An earlier draft also treated any head hit on a page under 20 KB as fatal. The
test suite caught that: a small but perfectly healthy page carrying 4/4
indicator groups plus a decoy string in an inline script was reported down. The
size heuristic is gone; the score decides.

Storefront detection uses four independent indicator groups and requires three.
All four are currently present on both sites with large margins, so a redesign,
a price change or a product swap cannot on its own trigger an alert.
"""

import html as _html
import re
from dataclasses import dataclass, field

from . import reasons

#: Bytes of the body treated as the "head region" for error-signature scanning.
HEAD_SCAN_BYTES = 4096

#: Below this, the response is not a page at all.
MIN_BODY_BYTES = 500

#: How many of the four indicator groups must be present.
REQUIRED_GROUPS = 3

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

#: (lower-cased needle, human label). Ordered most-specific first so the label
#: we report is the most informative one.
ERROR_SIGNATURES = (
    ("error establishing a database connection", "WordPress database connection error"),
    ("briefly unavailable for scheduled maintenance", "WordPress maintenance mode"),
    ("the site is experiencing technical difficulties", "WordPress critical error"),
    ("there has been a critical error on this website", "WordPress critical error"),
    ("attention required! | cloudflare", "Cloudflare firewall block"),
    ("checking your browser before accessing", "Cloudflare interstitial"),
    ("web server is down", "Cloudflare 521 - origin down"),
    ("origin is unreachable", "Cloudflare 523 - origin unreachable"),
    ("this account has been suspended", "Hosting account suspended"),
    ("bandwidth limit exceeded", "Hosting bandwidth limit exceeded"),
    ("503 service unavailable", "HTTP 503 error page"),
    ("service temporarily unavailable", "Service temporarily unavailable"),
    ("502 bad gateway", "HTTP 502 error page"),
    ("504 gateway", "HTTP 504 error page"),
    ("500 internal server error", "HTTP 500 error page"),
    ("internal server error", "Internal server error"),
    ("403 forbidden", "HTTP 403 error page"),
    ("404 not found", "HTTP 404 error page"),
    ("not acceptable!", "ModSecurity block"),
    ("mod_security", "ModSecurity block"),
    ("fatal error:", "PHP fatal error"),
    ("parse error:", "PHP parse error"),
    ("uncaught exception", "PHP uncaught exception"),
    ("warning: mysqli", "PHP MySQL warning"),
    ("under maintenance", "Maintenance page"),
    ("maintenance mode", "Maintenance page"),
    ("site is temporarily down", "Temporary downtime page"),
)

#: Site-independent indicator groups. Each group is satisfied by ANY member.
SHARED_INDICATOR_GROUPS = (
    ("shop", ("woocommerce", ">shop<", "/shop", "shop</a>", "product_cat")),
    (
        "price",
        (
            "woocommerce-price-amount",
            "woocommerce-price-currencysymbol",
            "&pound;",
            "&#163;",
            '"price"',
            "price</",
        ),
    ),
    (
        "basket",
        (
            "add to basket",
            "add to cart",
            "add_to_cart",
            "add-to-cart",
            "single_add_to_cart",
        ),
    ),
)

#: Total group count = brand + the shared groups above.
TOTAL_GROUPS = 1 + len(SHARED_INDICATOR_GROUPS)


@dataclass
class ValidationResult:
    ok: bool
    reason: str = reasons.OK
    detail: str = ""
    title: str = ""
    score: int = 0
    total_groups: int = TOTAL_GROUPS
    groups_present: tuple = field(default_factory=tuple)
    groups_missing: tuple = field(default_factory=tuple)


def extract_title(body: str) -> str:
    """Return the page <title>, unescaped and whitespace-collapsed."""
    match = _TITLE_RE.search(body)
    if not match:
        return ""
    text = _html.unescape(match.group(1))
    return re.sub(r"\s+", " ", text).strip()


def looks_like_html(body: str) -> bool:
    head = body[:HEAD_SCAN_BYTES].lstrip().lower()
    return head.startswith("<!doctype") or head.startswith("<html") or "<html" in head


def find_error_signature(body: str, title: str = None):
    """Return (label, where) for the first error signature found, else None.

    ``where`` is "title" or "head" and determines how much weight the caller
    gives the hit - see the module docstring.
    """
    if title is None:
        title = extract_title(body)
    title_lower = title.lower()
    head_lower = body[:HEAD_SCAN_BYTES].lower()

    for needle, label in ERROR_SIGNATURES:
        if needle in title_lower:
            return label, "title"
    for needle, label in ERROR_SIGNATURES:
        if needle in head_lower:
            return label, "head"
    return None


def storefront_groups(body_lower: str, brand_patterns):
    """Return (present, missing) tuples of indicator-group names."""
    present, missing = [], []

    if any(pattern in body_lower for pattern in brand_patterns):
        present.append("brand")
    else:
        missing.append("brand")

    for name, needles in SHARED_INDICATOR_GROUPS:
        if any(needle in body_lower for needle in needles):
            present.append(name)
        else:
            missing.append(name)

    return tuple(present), tuple(missing)


def validate_body(body: str, brand_patterns, content_type: str = "") -> ValidationResult:
    """Full body validation. ``body`` is the decoded response text."""
    if body is None or not body.strip():
        return ValidationResult(
            ok=False,
            reason=reasons.EMPTY_RESPONSE,
            detail="the response body was empty",
        )

    if content_type and "html" not in content_type.lower():
        return ValidationResult(
            ok=False,
            reason=reasons.NOT_HTML,
            detail=f"Content-Type was {content_type!r}",
        )

    if not looks_like_html(body):
        return ValidationResult(
            ok=False,
            reason=reasons.NOT_HTML,
            detail="no <html> or <!doctype> found at the start of the body",
        )

    title = extract_title(body)
    body_lower = body.lower()
    present, missing = storefront_groups(body_lower, brand_patterns)
    score = len(present)

    # The error-signature check runs BEFORE the size floor on purpose. Real
    # Apache/nginx/WordPress error pages are often only 200-400 bytes, so a
    # size check first would label a genuine "503 Service Unavailable" as
    # `empty_response`. The outage would still be caught, but the incident
    # would carry the wrong reason - and the reason is what the alert reports.
    signature = find_error_signature(body, title)
    if signature is not None:
        label, where = signature
        # A page that still renders the shop is NOT an error page, whatever
        # stray substrings it carries. Only a title hit, or a page that has
        # already lost its storefront content, counts.
        fatal = where == "title" or score < REQUIRED_GROUPS
        if fatal:
            return ValidationResult(
                ok=False,
                reason=reasons.ERROR_PAGE,
                detail=f"{label} (matched in {where}; page {len(body)} bytes, storefront score {score}/{TOTAL_GROUPS})",
                title=title,
                score=score,
                groups_present=present,
                groups_missing=missing,
            )

    if len(body.strip()) < MIN_BODY_BYTES:
        return ValidationResult(
            ok=False,
            reason=reasons.EMPTY_RESPONSE,
            detail=f"body was {len(body.strip())} bytes (minimum {MIN_BODY_BYTES})",
            title=title,
            score=score,
            groups_present=present,
            groups_missing=missing,
        )

    if score < REQUIRED_GROUPS:
        return ValidationResult(
            ok=False,
            reason=reasons.MISSING_STOREFRONT_INDICATORS,
            detail=f"only {score}/{TOTAL_GROUPS} indicator groups present; missing {', '.join(missing)}",
            title=title,
            score=score,
            groups_present=present,
            groups_missing=missing,
        )

    return ValidationResult(
        ok=True,
        reason=reasons.OK,
        detail=f"{score}/{TOTAL_GROUPS} indicator groups present",
        title=title,
        score=score,
        groups_present=present,
        groups_missing=missing,
    )
