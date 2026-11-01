import unittest
from datetime import datetime

from darkroom.domain import timezones


class OffsetTests(unittest.TestCase):
    def test_new_york_winter_and_summer(self):
        self.assertEqual(timezones.offset_minutes(40.7, -74.0, datetime(2024, 1, 15, 12)), -300)
        self.assertEqual(timezones.offset_minutes(40.7, -74.0, datetime(2024, 7, 15, 12)), -240)

    def test_a_half_hour_zone(self):
        self.assertEqual(timezones.offset_minutes(28.6, 77.2, datetime(2024, 1, 15, 12)), 330)

    def test_open_ocean_still_has_an_etc_zone_or_none(self):
        found = timezones.offset_minutes(0.5, -30.0, datetime(2024, 1, 15, 12))
        self.assertTrue(found is None or found % 60 == 0)

    def test_no_zone_at_that_point(self):
        from unittest import mock

        with mock.patch.object(timezones, "finder") as finder:
            finder.return_value.timezone_at.return_value = None
            self.assertIsNone(timezones.offset_minutes(1.0, 2.0, datetime(2024, 1, 15, 12)))

    def test_an_unknown_zone_name(self):
        from unittest import mock

        with mock.patch.object(timezones, "finder") as finder:
            finder.return_value.timezone_at.return_value = "Nowhere/Land"
            self.assertIsNone(timezones.offset_minutes(1.0, 2.0, datetime(2024, 1, 15, 12)))

    def test_the_finder_is_built_once(self):
        self.assertIs(timezones.finder(), timezones.finder())


if __name__ == "__main__":
    unittest.main()
