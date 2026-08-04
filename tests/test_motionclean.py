"""Tests for reachy_mini_link.motionclean (pure python, no bpy needed)."""

import math
import random
import unittest

from reachy_mini_link import motionclean as mc


class UnwrapTest(unittest.TestCase):
    def test_passthrough_when_continuous(self):
        vals = [0.0, 0.1, 0.2, 0.15]
        self.assertEqual(mc.unwrap(vals), vals)

    def test_removes_positive_jump(self):
        # 3.1 -> -3.1 is a wrap, not a -6.2 rad swing.
        out = mc.unwrap([3.0, 3.1, -3.1, -3.0])
        self.assertAlmostEqual(out[2], -3.1 + 2 * math.pi)
        self.assertAlmostEqual(out[3], -3.0 + 2 * math.pi)

    def test_removes_negative_jump(self):
        out = mc.unwrap([-3.0, -3.1, 3.1, 3.0])
        self.assertAlmostEqual(out[2], 3.1 - 2 * math.pi)

    def test_empty(self):
        self.assertEqual(mc.unwrap([]), [])


class GaussianSmoothTest(unittest.TestCase):
    def test_sigma_zero_is_identity(self):
        t = [0.0, 0.1, 0.2]
        v = [1.0, 5.0, -2.0]
        self.assertEqual(mc.gaussian_smooth(t, v, 0.0), v)

    def test_constant_signal_untouched(self):
        t = [i * 0.01 for i in range(100)]
        v = [2.5] * 100
        out = mc.gaussian_smooth(t, v, 0.05)
        for x in out:
            self.assertAlmostEqual(x, 2.5, places=9)

    def test_reduces_noise_keeps_trend(self):
        rng = random.Random(42)
        t = [i * 0.01 for i in range(500)]
        clean_sig = [math.sin(2 * math.pi * 0.5 * x) for x in t]  # 0.5 Hz
        noisy = [c + rng.gauss(0.0, 0.05) for c in clean_sig]
        out = mc.gaussian_smooth(t, noisy, 0.03)
        err_noisy = max(abs(a - b) for a, b in zip(noisy, clean_sig))
        err_smooth = max(abs(a - b) for a, b in zip(out, clean_sig))
        self.assertLess(err_smooth, err_noisy * 0.7)
        self.assertLess(err_smooth, 0.08)

    def test_irregular_timestamps(self):
        # A dense cluster must not dominate the average at a far sample.
        t = [0.0, 0.001, 0.002, 0.003, 1.0]
        v = [10.0, 10.0, 10.0, 10.0, 0.0]
        out = mc.gaussian_smooth(t, v, 0.02)
        # The last sample is > 3 sigma from the cluster: stays its own value.
        self.assertAlmostEqual(out[-1], 0.0, places=9)


class RdpTest(unittest.TestCase):
    def test_straight_line_keeps_endpoints_only(self):
        t = [i * 0.1 for i in range(50)]
        v = [3.0 * x + 1.0 for x in t]
        self.assertEqual(mc.rdp(t, v, 1e-6), [0, 49])

    def test_epsilon_zero_keeps_all(self):
        t = [0.0, 0.1, 0.2, 0.3]
        v = [0.0, 1.0, 0.0, 1.0]
        self.assertEqual(mc.rdp(t, v, 0.0), [0, 1, 2, 3])

    def test_keeps_corner(self):
        t = [0.0, 1.0, 2.0]
        v = [0.0, 1.0, 0.0]
        self.assertEqual(mc.rdp(t, v, 0.5), [0, 1, 2])

    def test_error_bound_holds(self):
        rng = random.Random(7)
        t = [i * 0.01 for i in range(600)]
        v = [math.sin(2 * math.pi * 0.8 * x) + 0.3 * math.sin(2 * math.pi * 2.7 * x)
             for x in t]
        eps = 0.01
        idx = mc.rdp(t, v, eps)
        # Reconstruct by linear interpolation over kept samples and check
        # every dropped sample is within eps.
        kt = [t[i] for i in idx]
        kv = [v[i] for i in idx]

        def interp(x):
            for a in range(len(kt) - 1):
                if kt[a] <= x <= kt[a + 1]:
                    f = (x - kt[a]) / (kt[a + 1] - kt[a])
                    return kv[a] + f * (kv[a + 1] - kv[a])
            return kv[-1]

        worst = max(abs(interp(x) - y) for x, y in zip(t, v))
        self.assertLessEqual(worst, eps + 1e-12)
        # And it should actually compress a smooth signal.
        self.assertLess(len(idx), len(t) // 2)

    def test_short_series(self):
        self.assertEqual(mc.rdp([0.0], [1.0], 0.1), [0])
        self.assertEqual(mc.rdp([0.0, 1.0], [1.0, 2.0], 0.1), [0, 1])


class CleanTest(unittest.TestCase):
    def test_angular_wrap_survives_pipeline(self):
        # A steady rotation crossing ±π must come out as one continuous
        # ramp (monotonic, full span), not a sawtooth with 2π drops.
        t = [i * 0.02 for i in range(300)]
        raw = [math.atan2(math.sin(0.8 * x + 3.0), math.cos(0.8 * x + 3.0))
               for x in t]  # wrapped angle sweeping through ±π
        kt, kv = mc.clean(t, raw, sigma=0.03, epsilon=0.002, angular=True)
        deltas = [b - a for a, b in zip(kv, kv[1:])]
        self.assertTrue(all(d > 0 for d in deltas))          # no 2π drops
        self.assertAlmostEqual(kv[-1] - kv[0], 0.8 * (t[-1] - t[0]), places=1)
        self.assertLess(len(kt), 30)                          # a ramp is cheap

    def test_endpoints_preserved(self):
        t = [i * 0.01 for i in range(200)]
        v = [math.cos(3 * x) for x in t]
        kt, kv = mc.clean(t, v, sigma=0.02, epsilon=0.005)
        self.assertEqual(kt[0], t[0])
        self.assertEqual(kt[-1], t[-1])


if __name__ == "__main__":
    unittest.main()
