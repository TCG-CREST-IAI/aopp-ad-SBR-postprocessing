import itertools
import sys
import time
import unittest
from decimal import Decimal, localcontext
from pathlib import Path

import numpy as np
from scipy import sparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bp import PaperStates, boxplus, decode, decode_result


def reference(H, s, r0, iterations):
    edges = list(zip(*np.nonzero(H)))
    outgoing = {(row, col): r0[col] for row, col in edges}
    result = []
    for _ in range(iterations):
        incoming = {}
        for row, col in edges:
            other = [outgoing[row, j] for j in np.flatnonzero(H[row]) if j != col]
            incoming[row, col] = (-1 if s[row] else 1) * 2 * np.arctanh(np.prod(np.tanh(np.array(other) / 2)))
        r = r0.copy()
        for row, col in edges:
            r[col] += incoming[row, col]
        outgoing = {(row, col): r0[col] + sum(incoming[j, col] for j in np.flatnonzero(H[:, col]) if j != row) for row, col in edges}
        result.append(r)
    return result


class BPTests(unittest.TestCase):
    def test_native_matches_independent_equations(self):
        rng = np.random.default_rng(161203673)
        for case in range(100):
            H = np.zeros((4, 9), dtype=np.uint8)
            for row in H:
                row[rng.choice(9, 4, replace=False)] = 1
            s = rng.integers(0, 2, 4, dtype=np.uint8)
            r0 = rng.uniform(.2, 2.5, 9) * rng.choice([-1, 1], 9)
            r0[case % 9] = 0
            states = PaperStates(sparse.csr_matrix(H), s, r0)
            for k, expected in enumerate(reference(H, s, r0, 8), 1):
                np.testing.assert_allclose(states.step(k), expected, atol=1e-11, rtol=1e-10)

    def test_stable_boxplus_against_high_precision(self):
        for a, b in [(100, 100), (-100, 100), (100, .1), (-2, -3), (0, 100)]:
            with localcontext() as ctx:
                ctx.prec = 100
                x, y = Decimal(str(a)).exp(), Decimal(str(b)).exp()
                product = (x - 1) / (x + 1) * (y - 1) / (y + 1)
                expected = ((1 + product) / (1 - product)).ln()
            self.assertAlmostEqual(boxplus(a, b), float(expected), places=12)

    def test_shortened_bit_numerical_fix(self):
        result = decode_result([1], [0, 0], [[1, 1]], .1, [0], [], 1)
        np.testing.assert_array_equal(result.error, [0, 1])
        self.assertEqual(result.backend, "stable sum-product")

    def test_zero_decision_and_five_previous_iterations(self):
        z, D = decode([0], [0, 0, 0], [[1, 1, 1]], .1, [], [0, 1, 2], 1)
        np.testing.assert_array_equal(z, [0, 0, 0])
        self.assertIsNone(D)
        result = decode_result([1], [0, 0, 0], [[1, 1, 1]], .1, [], [0, 1, 2], 2)
        self.assertIsNone(result.error)
        self.assertEqual(result.iterations, 5)
        np.testing.assert_array_equal(result.positions, [0, 1])

    def test_deadline_aborts_without_a_decoding_result(self):
        with self.assertRaises(TimeoutError):
            decode_result([1], [0, 0, 0], [[1, 1, 1]], .1, [], [], 1, time.monotonic() - 1)

    def test_all_shortened_can_succeed_before_confidence_rule(self):
        z, D = decode([1], [0, 1], [[1, 1]], .1, [0, 1], [], 1)
        np.testing.assert_array_equal(z, [0, 1])
        self.assertIsNone(D)
