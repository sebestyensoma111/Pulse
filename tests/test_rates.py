import unittest

from pulse.core.monitor import Rates


class RatesTests(unittest.TestCase):
    def setUp(self):
        self.rates = Rates()

    def test_first_sample_sets_baseline(self):
        self.assertEqual(self.rates.sample({"a": (100, 200)}, 10), (None, None))
        self.assertEqual(self.rates.sample({"a": (160, 240)}, 12), (30, 20))

    def test_rates_sum_devices_and_preserve_zero(self):
        self.rates.sample({"a": (100, 200), "b": (400, 500)}, 10)
        self.assertEqual(
            self.rates.sample({"a": (130, 200), "b": (460, 500)}, 13),
            (30, 0),
        )

    def test_reset_is_ignored_only_for_affected_counter(self):
        self.rates.sample({"a": (100, 200), "b": (400, 500)}, 10)
        self.assertEqual(
            self.rates.sample({"a": (10, 240), "b": (460, 20)}, 12),
            (30, 20),
        )

    def test_all_resets_leave_rate_unavailable(self):
        self.rates.sample({"a": (100, 200)}, 10)
        self.assertEqual(self.rates.sample({"a": (10, 20)}, 11), (None, None))
        self.assertEqual(self.rates.sample({"a": (20, 40)}, 12), (10, 20))

    def test_devices_need_two_consecutive_samples(self):
        self.rates.sample({"old": (100, 200), "same": (10, 20)}, 10)
        self.assertEqual(
            self.rates.sample({"new": (900, 800), "same": (20, 40)}, 11),
            (10, 20),
        )
        self.assertEqual(self.rates.sample({"new": (910, 840)}, 12), (10, 40))

    def test_missing_sample_clears_baseline(self):
        self.rates.sample({"a": (100, 200)}, 10)
        self.assertEqual(self.rates.sample(None, 11), (None, None))
        self.assertEqual(self.rates.sample({"a": (150, 220)}, 12), (None, None))
        self.assertEqual(self.rates.sample({"a": (160, 240)}, 13), (10, 20))

    def test_nonpositive_elapsed_time_has_no_rate(self):
        self.rates.sample({"a": (100, 200)}, 10)
        self.assertEqual(self.rates.sample({"a": (110, 220)}, 10), (None, None))
        self.assertEqual(self.rates.sample({"a": (120, 240)}, 9), (None, None))
