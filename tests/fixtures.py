"""Static HTML fixtures. No network access anywhere in the test suite."""

_FILLER = (
    "<p>Bulk trade packaging and catering supplies for street-food pitches, "
    "delivered across the UK. Wholesale cups, lids, bags, seasonings and "
    "equipment for corn and chips operators.</p>\n" * 6
)


def _page(title, body, filler=True):
    return (
        "<!DOCTYPE html>\n<html lang=\"en-GB\">\n<head>\n"
        f"<meta charset=\"UTF-8\" />\n<title>{title}</title>\n</head>\n<body>\n"
        f"{body}\n{_FILLER if filler else ''}\n</body>\n</html>\n"
    )


#: A healthy storefront carrying all four indicator groups.
HEALTHY_DIRECT_PACKAGING = _page(
    "Shop - Direct Packaging &amp; Solutions LTD",
    """
<header><a href="/shop/">Shop</a><a href="/basket/">Basket</a></header>
<h1>Direct Packaging &amp; Solutions LTD</h1>
<ul class="products columns-4">
  <li class="product type-product">
    <a href="/product/kraft-box/" class="woocommerce-LoopProduct-link">
      <h2 class="woocommerce-loop-product__title">Kraft Takeaway Box</h2>
      <span class="woocommerce-Price-amount amount">
        <bdi><span class="woocommerce-Price-currencySymbol">&pound;</span>24.99</bdi>
      </span>
    </a>
    <a href="?add-to-cart=101" class="button product_type_simple add_to_cart_button">Add to basket</a>
  </li>
</ul>
""",
)

#: Same, but with the exact decoy strings measured on the REAL homepages.
#: "503", "not found" and "cloudflare" all occur on healthy pages in inline
#: scripts. This fixture is the regression guard for that finding.
HEALTHY_WITH_DECOY_STRINGS = HEALTHY_DIRECT_PACKAGING.replace(
    "</body>",
    """
<script>
  var errorMessages = {
    503: "Service temporarily unavailable",
    404: "not found",
    502: "bad gateway"
  };
  var cdn = "cloudflare";
  var maintenance_mode = false;
  window.wcSettings = { i18n: { notFound: "not found" } };
</script>
</body>""",
)

HEALTHY_MR_CORN = _page(
    "MR Corn",
    """
<header><a href="/shop/">Shop</a></header>
<h1>MR Corn</h1>
<ul class="products">
  <li class="product">
    <h2>BBQ Seasoning 1kg</h2>
    <span class="woocommerce-Price-amount amount">
      <bdi><span class="woocommerce-Price-currencySymbol">&pound;</span>9.50</bdi>
    </span>
    <a href="?add-to-cart=1696" class="add_to_cart_button">Add to basket</a>
  </li>
</ul>
""",
)

# --- Error pages ------------------------------------------------------------

WP_DATABASE_ERROR = (
    "<!DOCTYPE html><html><head><title>Database Error</title></head><body>"
    "<h1>Error establishing a database connection</h1>"
    "<p>This either means that the username and password information is incorrect.</p>"
    "</body></html>"
)

WP_MAINTENANCE = (
    "<!DOCTYPE html><html><head><title>Maintenance</title></head><body>"
    "<h1>Briefly unavailable for scheduled maintenance. Check back in a minute.</h1>"
    "</body></html>"
)

WP_CRITICAL_ERROR = (
    "<!DOCTYPE html><html><head><title>Site error</title></head><body>"
    "<p>There has been a critical error on this website.</p>"
    "</body></html>"
)

CLOUDFLARE_BLOCK = (
    "<!DOCTYPE html><html><head><title>Attention Required! | Cloudflare</title></head>"
    "<body><h1>Sorry, you have been blocked</h1></body></html>"
)

CLOUDFLARE_ORIGIN_DOWN = (
    "<!DOCTYPE html><html><head><title>directpackaging.online | 521: Web server is down</title>"
    "</head><body><h1>Web server is down</h1></body></html>"
)

APACHE_503 = (
    "<!DOCTYPE html><html><head><title>503 Service Unavailable</title></head>"
    "<body><h1>503 Service Unavailable</h1><p>The server is temporarily unable to service "
    "your request due to maintenance downtime.</p></body></html>"
)

PHP_FATAL = (
    "<!DOCTYPE html><html><head><title>Error</title></head><body>"
    "Fatal error: Uncaught Error: Call to undefined function in /home/site/wp-content/"
    "plugins/example/example.php on line 42"
    "</body></html>"
)

ACCOUNT_SUSPENDED = (
    "<!DOCTYPE html><html><head><title>Account Suspended</title></head>"
    "<body><h1>This Account has been suspended</h1></body></html>"
)

#: A LARGE error page: no storefront markup at all, so the score-0 branch must
#: catch it even though it is well over the small-page threshold.
LARGE_ERROR_PAGE = _page(
    "Something went wrong",
    "<h1>Internal Server Error</h1>" + ("<p>Please try again later.</p>" * 900),
    filler=False,
)

# --- Non-error but invalid --------------------------------------------------

#: Correct brand, but the shop itself has vanished (theme/plugin failure).
BRAND_ONLY_NO_SHOP = _page(
    "Direct Packaging",
    "<h1>Direct Packaging &amp; Solutions LTD</h1><p>Welcome.</p>",
)

JSON_BODY = '{"status":"ok","message":"this is not html at all, but it is long enough to pass the size floor. ' + ("x" * 600) + '"}'

EMPTY_BODY = "   \n  "

TINY_BODY = "<html><body>ok</body></html>"
