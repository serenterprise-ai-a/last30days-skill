"""Tests for the configurable lookback window (default 30, up to 365 days)."""

import io
import unittest
from contextlib import redirect_stderr

import last30days as cli
from lib import dates, signals, schema


class TestClampLookbackDays(unittest.TestCase):
    def test_in_range_passthrough(self):
        for d in (1, 30, 90, 200, 365):
            self.assertEqual(cli.clamp_lookback_days(d), d)

    def test_below_minimum_clamps_to_one_with_warning(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.assertEqual(cli.clamp_lookback_days(0), 1)
        self.assertIn("minimum", buf.getvalue())

    def test_above_maximum_clamps_to_365_with_warning(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.assertEqual(cli.clamp_lookback_days(9999), 365)
        self.assertIn("365", buf.getvalue())

    def test_default_flag_value_is_30(self):
        args, _ = cli.build_parser().parse_known_args(["some topic"])
        self.assertEqual(args.lookback_days, 30)

    def test_days_flag_parses(self):
        args, _ = cli.build_parser().parse_known_args(["topic", "--days", "180"])
        self.assertEqual(args.lookback_days, 180)


class TestDaysBetween(unittest.TestCase):
    def test_basic_span(self):
        self.assertEqual(dates.days_between("2026-01-01", "2026-01-31"), 30)

    def test_year_span(self):
        self.assertEqual(dates.days_between("2025-06-14", "2026-06-14"), 365)

    def test_unparseable_returns_none(self):
        self.assertIsNone(dates.days_between("not-a-date", "2026-01-01"))


class TestFreshnessScalesToWindow(unittest.TestCase):
    """An item older than 30 days should be 'ancient' in a 30-day search but
    still carry recency signal in a year-long search."""

    def _item(self, published_at):
        it = schema.SourceItem(
            item_id="x1", source="reddit", title="t", body="b", url="u",
            author="a", published_at=published_at,
        )
        return it

    def test_60_day_old_item_zero_in_30d_window(self):
        dates_60_ago = dates.get_date_range(60)[0]  # 60 days before today
        score = signals.freshness(self._item(dates_60_ago), "strict_recent", lookback_days=30)
        self.assertEqual(score, 0)

    def test_60_day_old_item_positive_in_365d_window(self):
        dates_60_ago = dates.get_date_range(60)[0]
        score = signals.freshness(self._item(dates_60_ago), "strict_recent", lookback_days=365)
        self.assertGreater(score, 0)

    def test_default_window_matches_legacy_behaviour(self):
        recent = dates.get_date_range(5)[0]  # 5 days ago
        default = signals.freshness(self._item(recent), "strict_recent")
        explicit = signals.freshness(self._item(recent), "strict_recent", lookback_days=30)
        self.assertEqual(default, explicit)


if __name__ == "__main__":
    unittest.main()
