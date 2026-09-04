"""Static configuration: what we monitor and what a healthy page looks like.

Nothing here is a secret. These are two public URLs and the markup patterns a
healthy WooCommerce storefront emits. No credentials are read, stored or
transmitted anywhere in this project.
"""

from dataclasses import dataclass, field

# --- Request behaviour ------------------------------------------------------

#: Total wall-clock budget for one site check, across every redirect hop.
TOTAL_TIMEOUT_SECONDS = 30.0

#: Per-connection socket timeout. Lower than the total so a single stalled hop
#: cannot consume the whole budget.
CONNECT_TIMEOUT_SECONDS = 15.0

#: Redirect hops allowed before we call it a redirect problem.
MAX_REDIRECTS = 5

#: Hard cap on bytes read from a response. The storefronts are ~600-750 KB;
#: 3 MB is generous headroom while still bounding memory and bandwidth.
MAX_BODY_BYTES = 3 * 1024 * 1024

#: Attempts (1 = no retry) for transient network classes only.
MAX_ATTEMPTS = 3

#: Backoff before attempt 2 and attempt 3, in seconds.
RETRY_BACKOFF_SECONDS = (2.0, 5.0)

#: Clearly identified, honest monitoring User-Agent. Verified 2026-09-04 as
#: accepted by both sites' WAFs. A bare "Mozilla/5.0" is rejected by MR Corn's
#: ModSecurity rules, so do not "simplify" this string.
USER_AGENT = (
    "WebsiteUptimeMonitor/1.0 "
    "(+https://github.com/kkiran2025/website-uptime-monitor; "
    "read-only availability check; 1 GET per site per run)"
)

REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-GB,en;q=0.9",
    # Ask for an uncompressed body so we never have to decompress untrusted
    # bytes just to read a title.
    "Accept-Encoding": "identity",
    "Cache-Control": "no-cache",
    "Connection": "close",
}


# --- Sites ------------------------------------------------------------------


@dataclass(frozen=True)
class Site:
    """One monitored storefront."""

    key: str
    name: str
    url: str
    #: Per-site GitHub issue label, alongside the shared ALERT_LABEL.
    label: str
    #: Lower-cased substrings, any one of which proves the brand is on the page.
    brand_patterns: tuple = field(default_factory=tuple)

    @property
    def issue_labels(self) -> list:
        return [ALERT_LABEL, self.label]


#: Shared label applied to every incident issue.
ALERT_LABEL = "uptime-alert"

#: Colours + descriptions used when creating the labels (issues: write).
LABEL_DEFINITIONS = {
    ALERT_LABEL: ("b60205", "Automated uptime incident"),
    "uptime-direct-packaging": ("0e8a16", "Incident on directpackaging.online"),
    "uptime-mr-corn": ("fbca04", "Incident on mrcorn.uk.com"),
}

SITES = (
    Site(
        key="direct-packaging",
        name="Direct Packaging",
        url="https://directpackaging.online/",
        label="uptime-direct-packaging",
        brand_patterns=("direct packaging", "directpackaging"),
    ),
    Site(
        key="mr-corn",
        name="MR Corn",
        url="https://mrcorn.uk.com/",
        label="uptime-mr-corn",
        brand_patterns=("mr corn", "mrcorn"),
    ),
)


def site_by_key(key: str):
    for site in SITES:
        if site.key == key:
            return site
    return None
