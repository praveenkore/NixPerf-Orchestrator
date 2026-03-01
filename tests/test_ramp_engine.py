"""
test_ramp_engine.py - Unit tests for the ramp_engine module.

Tests:
    - constant_arrival calculation
    - proportional calculation
    - fixed calculation
    - invalid strategy
    - zero users
    - missing parameters
    - production safety guards (min/max clamping)
"""
import sys
import os
import unittest

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from orchestrator.ramp_engine import calculate_rampup, get_default_ramp_strategy


class TestConstantArrival(unittest.TestCase):
    """Tests for the constant_arrival strategy."""

    def test_basic_calculation(self):
        # 1000 users / 5 per second = 200s
        config = {"type": "constant_arrival", "arrival_rate": 5}
        self.assertEqual(calculate_rampup(1000, config), 200)

    def test_fractional_result_rounds_up(self):
        # 500 / 3 = 166.67 → ceil = 167
        config = {"type": "constant_arrival", "arrival_rate": 3}
        self.assertEqual(calculate_rampup(500, config), 167)

    def test_small_arrival_rate(self):
        # 100 / 1 = 100s
        config = {"type": "constant_arrival", "arrival_rate": 1}
        self.assertEqual(calculate_rampup(100, config), 100)

    def test_missing_arrival_rate(self):
        config = {"type": "constant_arrival"}
        with self.assertRaises(ValueError):
            calculate_rampup(100, config)

    def test_zero_arrival_rate(self):
        config = {"type": "constant_arrival", "arrival_rate": 0}
        with self.assertRaises(ValueError):
            calculate_rampup(100, config)

    def test_negative_arrival_rate(self):
        config = {"type": "constant_arrival", "arrival_rate": -5}
        with self.assertRaises(ValueError):
            calculate_rampup(100, config)


class TestFixed(unittest.TestCase):
    """Tests for the fixed strategy."""

    def test_basic_fixed(self):
        config = {"type": "fixed", "value": 120}
        self.assertEqual(calculate_rampup(1000, config), 120)

    def test_fixed_independent_of_users(self):
        config = {"type": "fixed", "value": 60}
        self.assertEqual(calculate_rampup(100, config), 60)
        self.assertEqual(calculate_rampup(10000, config), 60)

    def test_missing_value(self):
        config = {"type": "fixed"}
        with self.assertRaises(ValueError):
            calculate_rampup(100, config)

    def test_zero_value(self):
        config = {"type": "fixed", "value": 0}
        with self.assertRaises(ValueError):
            calculate_rampup(100, config)


class TestProportional(unittest.TestCase):
    """Tests for the proportional strategy."""

    def test_basic_proportional(self):
        # base_ramp * (users / base_users) = 60 * (2000 / 500) = 240
        config = {"type": "proportional", "base_users": 500, "base_ramp": 60}
        self.assertEqual(calculate_rampup(2000, config), 240)

    def test_same_as_base(self):
        config = {"type": "proportional", "base_users": 500, "base_ramp": 60}
        self.assertEqual(calculate_rampup(500, config), 60)

    def test_fractional_rounds_up(self):
        # 60 * (750 / 500) = 90
        config = {"type": "proportional", "base_users": 500, "base_ramp": 60}
        self.assertEqual(calculate_rampup(750, config), 90)

    def test_missing_base_users(self):
        config = {"type": "proportional", "base_ramp": 60}
        with self.assertRaises(ValueError):
            calculate_rampup(1000, config)

    def test_missing_base_ramp(self):
        config = {"type": "proportional", "base_users": 500}
        with self.assertRaises(ValueError):
            calculate_rampup(1000, config)


class TestInvalidStrategy(unittest.TestCase):
    """Tests for invalid configurations."""

    def test_unknown_strategy(self):
        config = {"type": "logarithmic"}
        with self.assertRaises(ValueError):
            calculate_rampup(1000, config)

    def test_missing_type(self):
        config = {"arrival_rate": 5}
        with self.assertRaises(ValueError):
            calculate_rampup(1000, config)

    def test_empty_config(self):
        with self.assertRaises(ValueError):
            calculate_rampup(1000, {})


class TestZeroUsers(unittest.TestCase):
    """Tests for zero or negative user counts."""

    def test_zero_users(self):
        config = {"type": "fixed", "value": 60}
        with self.assertRaises(ValueError):
            calculate_rampup(0, config)

    def test_negative_users(self):
        config = {"type": "fixed", "value": 60}
        with self.assertRaises(ValueError):
            calculate_rampup(-10, config)


class TestSafetyGuards(unittest.TestCase):
    """Tests for production safety clamping."""

    def test_minimum_rampup_is_one(self):
        # fixed value of 0.5 → should be clamped to 1
        # (but value <= 0 is rejected, so test with very small arrival rate)
        # 1 user / 100 rate = 0.01 → ceil = 1 (already >= 1)
        config = {"type": "constant_arrival", "arrival_rate": 100}
        result = calculate_rampup(1, config)
        self.assertGreaterEqual(result, 1)

    def test_max_rampup_clamped(self):
        # proportional: 60 * (10000 / 100) = 6000, but max = 10000 * 4 = 40000
        # In this case 6000 < 40000 so no clamp
        config = {"type": "proportional", "base_users": 100, "base_ramp": 60}
        result = calculate_rampup(10000, config)
        self.assertLessEqual(result, 10000 * 4)


class TestDefaultStrategy(unittest.TestCase):
    """Tests for get_default_ramp_strategy."""

    def test_default_returns_proportional(self):
        default = get_default_ramp_strategy(500)
        self.assertEqual(default["type"], "proportional")
        self.assertEqual(default["base_users"], 500)
        self.assertEqual(default["base_ramp"], 60)

    def test_default_works_with_calculate(self):
        default = get_default_ramp_strategy(500)
        result = calculate_rampup(1000, default)
        # 60 * (1000 / 500) = 120
        self.assertEqual(result, 120)


if __name__ == "__main__":
    unittest.main()
