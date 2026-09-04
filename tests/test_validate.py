"""Content-validation tests - the layer that decides up vs down."""

import unittest

from monitor import reasons
from monitor.config import site_by_key
from monitor.validate import (
    REQUIRED_GROUPS,
    TOTAL_GROUPS,
    extract_title,
    find_error_signature,
    looks_like_html,
    storefront_groups,
    validate_body,
)
from tests import fixtures

DP = site_by_key("direct-packaging")
MC = site_by_key("mr-corn")


class TestTitle(unittest.TestCase):
    def test_extracts_and_unescapes(self):
        self.assertEqual(
            extract_title(fixtures.HEALTHY_DIRECT_PACKAGING),
            "Shop - Direct Packaging & Solutions LTD",
        )

    def test_missing_title_is_empty(self):
        self.assertEqual(extract_title("<html><body>hi</body></html>"), "")

    def test_html_detection(self):
        self.assertTrue(looks_like_html(fixtures.HEALTHY_MR_CORN))
        self.assertFalse(looks_like_html(fixtures.JSON_BODY))


class TestHealthyPages(unittest.TestCase):
    def test_direct_packaging_passes(self):
        result = validate_body(fixtures.HEALTHY_DIRECT_PACKAGING, DP.brand_patterns, "text/html")
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.reason, reasons.OK)
        self.assertEqual(result.score, TOTAL_GROUPS)

    def test_mr_corn_passes(self):
        result = validate_body(fixtures.HEALTHY_MR_CORN, MC.brand_patterns, "text/html")
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.score, TOTAL_GROUPS)

    def test_decoy_strings_do_not_trigger_a_false_alarm(self):
        """Regression guard for the real-world measurement.

        Both live homepages contain '503', 'not found' and/or 'cloudflare' in
        inline scripts while being perfectly healthy. A naive whole-body
        substring rule would report them down. This must stay green.
        """
        result = validate_body(fixtures.HEALTHY_WITH_DECOY_STRINGS, DP.brand_patterns, "text/html")
        self.assertTrue(result.ok, f"false alarm on a healthy page: {result.reason} / {result.detail}")

    def test_a_price_change_alone_cannot_fail_the_check(self):
        page = fixtures.HEALTHY_MR_CORN.replace("9.50", "11.75")
        self.assertTrue(validate_body(page, MC.brand_patterns, "text/html").ok)

    def test_losing_one_indicator_group_still_passes(self):
        """Three of four is the bar, so one group may disappear in a redesign."""
        page = fixtures.HEALTHY_MR_CORN.replace("Add to basket", "Buy now").replace(
            "add_to_cart_button", "buy-now-button"
        ).replace("add-to-cart=1696", "buy=1696")
        result = validate_body(page, MC.brand_patterns, "text/html")
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.score, REQUIRED_GROUPS)


class TestErrorPages(unittest.TestCase):
    CASES = [
        ("database error", fixtures.WP_DATABASE_ERROR),
        ("maintenance", fixtures.WP_MAINTENANCE),
        ("wp critical error", fixtures.WP_CRITICAL_ERROR),
        ("cloudflare block", fixtures.CLOUDFLARE_BLOCK),
        ("cloudflare origin down", fixtures.CLOUDFLARE_ORIGIN_DOWN),
        ("apache 503", fixtures.APACHE_503),
        ("php fatal", fixtures.PHP_FATAL),
        ("account suspended", fixtures.ACCOUNT_SUSPENDED),
    ]

    def test_all_error_pages_are_detected(self):
        for label, page in self.CASES:
            with self.subTest(page=label):
                result = validate_body(page, DP.brand_patterns, "text/html")
                self.assertFalse(result.ok)
                self.assertEqual(
                    result.reason,
                    reasons.ERROR_PAGE,
                    f"{label} was flagged as {result.reason}, not error_page",
                )

    def test_large_error_page_caught_by_zero_storefront_score(self):
        page = fixtures.LARGE_ERROR_PAGE
        self.assertGreater(len(page), 20_000, "fixture must be large, so size cannot be the trigger")
        result = validate_body(page, DP.brand_patterns, "text/html")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, reasons.ERROR_PAGE)
        self.assertEqual(result.score, 0)

    def test_small_healthy_page_with_a_decoy_is_not_an_error_page(self):
        """The bug the suite caught: size must never override a full score."""
        page = fixtures.HEALTHY_WITH_DECOY_STRINGS
        self.assertLess(len(page), 20_000, "fixture must be small, to exercise the old failure")
        result = validate_body(page, DP.brand_patterns, "text/html")
        self.assertTrue(result.ok, result.detail)

    def test_signature_in_title_is_reported_from_the_title(self):
        found = find_error_signature(fixtures.APACHE_503)
        self.assertIsNotNone(found)
        self.assertEqual(found[1], "title")


class TestInvalidBodies(unittest.TestCase):
    def test_empty_body(self):
        result = validate_body(fixtures.EMPTY_BODY, DP.brand_patterns, "text/html")
        self.assertEqual(result.reason, reasons.EMPTY_RESPONSE)

    def test_tiny_body(self):
        result = validate_body(fixtures.TINY_BODY, DP.brand_patterns, "text/html")
        self.assertEqual(result.reason, reasons.EMPTY_RESPONSE)

    def test_json_content_type(self):
        result = validate_body(fixtures.JSON_BODY, DP.brand_patterns, "application/json")
        self.assertEqual(result.reason, reasons.NOT_HTML)

    def test_json_body_without_content_type(self):
        result = validate_body(fixtures.JSON_BODY, DP.brand_patterns, "")
        self.assertEqual(result.reason, reasons.NOT_HTML)

    def test_brand_present_but_shop_gone(self):
        result = validate_body(fixtures.BRAND_ONLY_NO_SHOP, DP.brand_patterns, "text/html")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, reasons.MISSING_STOREFRONT_INDICATORS)
        self.assertIn("brand", result.groups_present)

    def test_wrong_site_content_fails_on_brand(self):
        """MR Corn's page served from the Direct Packaging URL must not pass."""
        _, missing = storefront_groups(fixtures.HEALTHY_MR_CORN.lower(), DP.brand_patterns)
        self.assertIn("brand", missing)


if __name__ == "__main__":
    unittest.main()
