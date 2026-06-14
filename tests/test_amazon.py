"""Tests for amazon.py — Canopy-backed Amazon product/review source."""

import unittest
from unittest.mock import patch

from lib import amazon, normalize


SEARCH_PAYLOAD = {
    "success": True,
    "result": {
        "products": [
            {"asin": "B01AAA", "title": "Compact Standing Desk for Small Spaces",
             "url": "https://www.amazon.com/dp/B01AAA", "rating": 4.3, "ratingsTotal": 1200},
            {"asin": "B02BBB", "title": "Mini Adjustable Desk Riser",
             "rating": 4.0, "ratingsTotal": 800},
        ]
    },
}

REVIEWS_PAYLOAD = {
    "success": True,
    "result": {
        "reviews": [
            {"id": "r1", "title": "Wobbles at full height", "body": "Sturdy at sitting height but wobbles badly when raised to standing.",
             "rating": 2, "helpfulVotes": 47, "verifiedPurchase": True, "reviewer": {"name": "Dana"}, "date": "2025-11-02"},
            {"id": "r2", "title": "Too small", "body": "Desktop is shallower than advertised; my monitor barely fits.",
             "rating": 3, "helpfulVotes": 12, "verifiedPurchase": True, "reviewer": {"name": "Sam"}, "date": "2025-09-15"},
            {"id": "r3", "title": "", "body": "", "rating": 5, "helpfulVotes": 0},  # empty body -> dropped
        ]
    },
}


class TestRequestContract(unittest.TestCase):
    def test_no_token_short_circuits(self):
        from lib import http as http_module
        with patch.object(http_module, "get") as mock_get:
            res = amazon.search_amazon("standing desk", "2026-01-01", "2026-06-14", token=None)
            mock_get.assert_not_called()
            self.assertEqual(res["items"], [])
            self.assertIn("error", res)

    def test_search_uses_searchterm_param_and_api_key_header(self):
        from lib import http as http_module
        with patch.object(http_module, "get") as mock_get:
            mock_get.return_value = {"result": {"products": []}}
            amazon.search_products("standing desk for small apartments", "tok", count=5)
            params = mock_get.call_args.kwargs["params"]
            headers = mock_get.call_args.kwargs["headers"]
            self.assertIn("searchTerm", params)
            self.assertEqual(headers["API-KEY"], "tok")

    def test_reviews_query_by_asin(self):
        from lib import http as http_module
        with patch.object(http_module, "get") as mock_get:
            mock_get.return_value = {"result": {"reviews": []}}
            amazon.fetch_reviews("B01AAA", "tok", count=8)
            params = mock_get.call_args.kwargs["params"]
            self.assertEqual(params["asin"], "B01AAA")


class TestEndToEndShape(unittest.TestCase):
    def test_products_then_reviews_become_items(self):
        from lib import http as http_module

        def fake_get(url, **kwargs):
            if url.endswith("/search"):
                return SEARCH_PAYLOAD
            if url.endswith("/reviews"):
                return REVIEWS_PAYLOAD
            raise AssertionError(f"unexpected url {url}")

        with patch.object(http_module, "get", side_effect=fake_get):
            res = amazon.search_amazon("standing desk for small apartments",
                                       "2026-06-01", "2026-06-14", depth="quick", token="tok")
        items = res["items"]
        # 2 products x 2 non-empty reviews each = 4 items
        self.assertEqual(len(items), 4)
        first = items[0]
        self.assertIn("Compact Standing Desk", first["title"])
        self.assertEqual(first["engagement"]["helpful_votes"], 47)
        self.assertEqual(first["metadata"]["asin"], "B01AAA")
        self.assertTrue(first["body"])
        # most-helpful sorted first within a product
        self.assertGreaterEqual(items[0]["engagement"]["helpful_votes"],
                                items[1]["engagement"]["helpful_votes"])

    def test_reviews_are_evergreen_after_normalize(self):
        """Reviews must survive the date-window filter even when older than the
        window — i.e. normalized published_at is None."""
        from lib import http as http_module

        def fake_get(url, **kwargs):
            return SEARCH_PAYLOAD if url.endswith("/search") else REVIEWS_PAYLOAD

        with patch.object(http_module, "get", side_effect=fake_get):
            res = amazon.search_amazon("standing desk", "2026-06-01", "2026-06-14",
                                       depth="quick", token="tok")
        raw = amazon.parse_amazon_response(res)
        # A tight recent window that excludes the 2025 review dates:
        normalized = normalize.normalize_source_items("amazon", raw, "2026-06-01", "2026-06-14")
        self.assertEqual(len(normalized), len(raw))  # nothing dropped by date filter
        self.assertTrue(all(n.published_at is None for n in normalized))
        # the real review date is preserved in metadata
        self.assertTrue(any(n.metadata.get("review_date") for n in normalized))


if __name__ == "__main__":
    unittest.main()
