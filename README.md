# website-uptime-monitor

Read-only uptime monitoring for two live WooCommerce storefronts, running on
GitHub Actions and alerting through GitHub Issues so the alerts arrive as push
notifications on the GitHub mobile app.

| Website | URL |
| --- | --- |
| Direct Packaging | https://directpackaging.online/ |
| MR Corn | https://mrcorn.uk.com/ |

---

## Safety guarantees

This project is **strictly read-only** with respect to both websites. It:

- makes **public HTTP `GET` requests only** — the method is hard-coded, so
  nothing here can `POST`;
- **never logs in** to WordPress or anything else;
- **never adds to a basket**, submits a form, creates an account, places an
  order or touches a payment flow;
- **stores no passwords, cookies, credentials or API keys** — no cookie jar is
  installed, so no cookie is ever stored or sent;
- **changes nothing** on either website: no files, DNS, hosting, database,
  plugins, themes, settings or deployment configuration;
- lives in **its own repository**, entirely separate from both website
  codebases;
- sends roughly **1.3 MB per five-minute cycle** across both sites — one request
  each, which is negligible load.

The only credential used is GitHub's own ephemeral `GITHUB_TOKEN`, injected by
Actions at runtime and scoped to `contents: read` + `issues: write`.

---

## What "up" means here

A site passes only if **all** of the following hold.

| # | Check | Failure code |
| --- | --- | --- |
| 1 | The hostname resolves | `dns_failure` |
| 2 | TLS handshake and certificate validation succeed | `tls_failure` |
| 3 | A TCP connection is established | `connection_failure` |
| 4 | The whole exchange finishes within 30 seconds | `timeout` |
| 5 | Redirects never revisit a URL | `redirect_loop` |
| 6 | At most 5 hops, a `Location` header is present, no HTTPS→HTTP downgrade | `redirect_problem` |
| 7 | The final status is `200` | `http_status` |
| 8 | The body is HTML | `not_html` |
| 9 | The body is not empty (≥ 500 bytes) | `empty_response` |
| 9b | The whole page arrived — bytes received match `Content-Length` | `truncated_response` |
| 10 | The page is not a server/maintenance/firewall/database/PHP/Cloudflare/WordPress error page | `error_page` |
| 11 | Expected storefront content is present | `missing_storefront_indicators` |

### Why the content checks do not cry wolf

Naive substring matching on the whole page **does not work**. Measured on
2026-09-04 against both perfectly healthy homepages:

- `directpackaging.online` contains `"503"` twice and `"not found"` three times
- `mrcorn.uk.com` contains `"cloudflare"` once

They live in inline JavaScript, product data and third-party snippets. So error
detection is constrained three ways:

1. only the `<title>` and the **first 4 KB** of the body are scanned — real
   error pages put their message at the very top;
2. a hit in the `<title>` is fatal on its own (a working shop never titles
   itself *"503 Service Unavailable"*);
3. a hit lower down is fatal **only** when the page has already dropped below
   the storefront bar. A page still rendering brand, shop, price and basket
   markup is a working shop, whatever stray substrings it contains.

> Rule 3 originally used page size instead of the storefront score. The test
> suite caught it: a small but perfectly healthy page carrying all four
> indicator groups plus a decoy string was reported down. `tests/test_validate.py`
> keeps that case green permanently.

Storefront detection uses **four independent indicator groups** — brand, shop /
WooCommerce markup, price markup, add-to-basket markup — and requires **three
of four**. All four are currently present on both sites by large margins, so a
redesign, a price change or a product swap cannot on its own raise an alert.

### Retries

Only genuinely transient classes are retried (`dns_failure`,
`connection_failure`, `timeout`, and 5xx responses): up to 3 attempts with 2 s
then 5 s backoff. **Content failures are never retried** — retrying those would
bury a real outage behind three identical attempts.

---

## Alerting

Labels: `uptime-alert` plus one per site — `uptime-direct-packaging`,
`uptime-mr-corn`. The keep-alive reminder (below) uses its own label,
`uptime-keepalive`, so it can never be mistaken for an outage.

| Situation | What happens |
| --- | --- |
| First failure | One issue is created and **assigned to the repository owner**, which is what triggers the phone notification |
| Failure continues | **Nothing.** No repeated issues, no repeated comments |
| Failure *class* changes (e.g. `timeout` → `http_status`) | One comment noting the change |
| Site recovers | One recovery comment including the outage duration, then the issue is **closed** |
| A later, separate outage | A **new** issue, because the previous one is closed |

Each incident records the website name, URL, detection time in **UTC and
Europe/London**, HTTP status, final URL after redirects, the concise failure
reason, and a link to the Actions run.

The open issue **is** the state — nothing is committed back to this repository,
which is why `contents: read` is sufficient.

> Issue bodies are deliberately terse. This repository is public, so page
> titles, stack traces, file paths and response bodies are never included.

---

## Running it locally

No dependencies beyond the Python 3 standard library.

```bash
# Check both sites. Never contacts GitHub, so no issue can be created.
python3 -m monitor.main --dry-run

# One site only
python3 -m monitor.main --dry-run --site mr-corn

# Machine-readable
python3 -m monitor.main --dry-run --json

# Full test suite (no network access required)
python3 -m unittest discover -s tests -t . -v
```

**Always use `--dry-run` locally.** Without it the script expects a
`GITHUB_TOKEN` and would act on real issues.

---

## Operational notes worth knowing

### Scheduled runs are far less frequent than the cron line says
GitHub schedules are best-effort, and on this repository "best-effort" means
**much** less often than every five minutes. Measured from the Actions history
for 2026-09-04 to 2026-10-19 (274 scheduled runs):

| Measure | Requested | Actually delivered |
| --- | --- | --- |
| Runs per day | 288 | **4–9** |
| Typical gap between runs | 5 min | **~3 h 50 min** |
| Longest gap | 5 min | **~9 h 21 min** |

So an outage can go unnoticed for several hours, and the outage duration in a
recovery comment is only accurate to within the gap between runs. Do not read
an outage into a late or missing run. A fix — an external timer that starts the
check through `workflow_dispatch` — is planned; until it lands, treat this
monitor as a coarse safety net, not a five-minute alarm.

### GitHub disables schedules after 60 days of repository inactivity
This is documented GitHub behaviour and the main way a monitor like this dies
quietly. **The workflow's own runs do not count as activity.** It is not worked
around automatically, because doing so would need `contents: write` and would
defeat the least-privilege permissions above.

Instead, every run reads the repository's last **push** time, as recorded by
GitHub (`contents: read` is enough). A commit's own date is not used, because
it is set by whoever made the commit. Once the last push is **45 days** old,
the monitor opens **one** `⏰ Keep-alive due` issue assigned to the owner — a
phone notification, about two weeks before the switch-off — and names the
expected switch-off date. It never repeats itself, and it closes the reminder
on the first run after a new push. A failure in this check never delays or
blocks an outage alert.

GitHub does not define what counts as "repository activity". The one action
known to reset the clock is **a commit pushed to `main`**, so do that at least
every 50 days. Last push: **2026-10-19**, so the next one is due by
**2026-12-08** (the hard cut-off is about 2026-12-18). If alerts go quiet for a
long stretch, check the Actions tab first — GitHub shows a banner when a
schedule has been disabled, and the workflow can be re-enabled from there.

### Cost
Actions minutes on **standard runners are free and unmetered for public
repositories**, so the five-minute cadence costs nothing. On a *private*
repository the same schedule would consume roughly **8,760 minutes/month**
against a GitHub Free allowance of 2,000 — the allowance would be exhausted
around day 7 of every month and checks would silently stop. That is why this
repository is public.

### Changing the cadence
Edit the `cron` line in
[`.github/workflows/uptime-monitor.yml`](.github/workflows/uptime-monitor.yml).
Cron is always **UTC**, so a UK-hours schedule shifts by an hour between GMT
and BST.

---

## Layout

```
monitor/
  config.py     sites, timeouts, the monitoring User-Agent
  reasons.py    the failure taxonomy
  validate.py   pure content validation (error pages, storefront indicators)
  check.py      HTTP layer: GET, manual redirect handling, retries
  alerts.py     GitHub issue lifecycle
  main.py       CLI entry point
tests/          unit tests, fixtures only, no network
.github/workflows/
  uptime-monitor.yml   the every-5-minutes check
  tests.yml            runs the suite on push
```
