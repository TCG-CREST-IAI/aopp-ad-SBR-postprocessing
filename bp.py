import time
from collections import deque
from dataclasses import dataclass

import numpy as np
from ldpc import BpDecoder
from scipy import sparse


@dataclass
class Result:
    error: object
    positions: object
    iterations: int
    confidence: list
    backend: str


def matrix(value):
    H = sparse.csr_matrix(value, copy=True)
    H.sum_duplicates()
    if min(H.shape) < 1 or not np.all((H.data == 0) | (H.data == 1)):
        raise ValueError("H must be a nonempty binary matrix")
    H.eliminate_zeros()
    return H.astype(np.uint8)


def bits(value, length):
    a = np.asarray(value)
    if a.shape != (length,) or not np.all((a == 0) | (a == 1)):
        raise ValueError(f"Expected {length} binary values")
    return a.astype(np.uint8, copy=True)


def boxplus(a, b):
    if a == 0 or b == 0:
        return 0.0
    if np.isinf(a):
        return float(np.sign(a) * b)
    if np.isinf(b):
        return float(np.sign(b) * a)
    x, y = abs(a), abs(b)
    value = min(x, y) + np.log1p(np.exp(-(x + y))) - np.log1p(np.exp(-abs(x - y)))
    return float(np.sign(a) * np.sign(b) * value)


class StableStates:
    def __init__(self, H, s, initial):
        self.H, self.s, self.initial = H, s, initial
        self.outgoing = initial[H.indices].copy()
        self.columns = [[] for _ in range(H.shape[1])]
        for edge, col in enumerate(H.indices):
            self.columns[col].append(edge)

    def step(self):
        incoming = np.empty_like(self.outgoing)
        for row in range(self.H.shape[0]):
            start, end = self.H.indptr[row:row + 2]
            values = self.outgoing[start:end]
            prefix, suffix = [float("inf")], [float("inf")] * (len(values) + 1)
            for value in values:
                prefix.append(boxplus(prefix[-1], value))
            for i in range(len(values) - 1, -1, -1):
                suffix[i] = boxplus(values[i], suffix[i + 1])
            for i in range(len(values)):
                incoming[start + i] = (-1 if self.s[row] else 1) * boxplus(prefix[i], suffix[i + 1])
        posterior = self.initial.copy()
        for col, edges in enumerate(self.columns):
            posterior[col] += sum(incoming[edge] for edge in edges)
            for edge in edges:
                self.outgoing[edge] = self.initial[col] + sum(incoming[other] for other in edges if other != edge)
        return posterior


class PaperStates:
    def __init__(self, H, s, initial):
        self.H, self.s, self.initial = H, s, initial
        self.sign = np.where(initial < 0, -1.0, 1.0)
        gauge = (initial < 0).astype(np.uint8)
        shifted = s ^ (np.asarray(H @ gauge).ravel() % 2)
        guarded = sparse.vstack([H, sparse.csr_matrix((1, H.shape[1]))], format="csr")
        t = np.exp(-np.abs(initial))
        self.engine = BpDecoder(guarded, error_channel=t / (1 + t), bp_method="product_sum", schedule="parallel", input_vector_type="syndrome", max_iter=1)
        self.syndrome = np.append(shifted, np.uint8(1))
        self.stable = None
        self.backend = "ldpc"

    def step(self, k):
        if self.stable is None:
            self.engine.max_iter = k
            self.engine.decode(self.syndrome)
            if self.engine.converge or self.engine.iter != k:
                raise RuntimeError("Unexpected native BP stopping behavior")
            llrs = self.sign * self.engine.log_prob_ratios
            if np.all(np.isfinite(llrs)):
                return llrs
            self.stable = StableStates(self.H, self.s, self.initial)
            self.backend = "stable sum-product"
            for _ in range(k - 1):
                self.stable.step()
        llrs = self.stable.step()
        if np.any(np.isnan(llrs)):
            raise FloatingPointError("Undefined LLR arithmetic")
        return llrs


def decode_result(s, e, H, q_est, S, P, d, deadline=None):
    H = matrix(H)
    n = H.shape[1]
    s, e = bits(s, H.shape[0]), bits(e, n)
    S, P = set(S), set(P)
    if S & P or any(type(i) is not int or not 0 <= i < n for i in S | P):
        raise ValueError("Invalid shortening or puncturing positions")
    if not 0 < q_est < 0.5 or not 1 <= d <= n:
        raise ValueError("Require 0 < q_est < 0.5 and 1 <= d <= n")
    mask = np.array([i not in S for i in range(n)])
    initial = (1 - 2 * e.astype(float)) * (np.log1p(-q_est) - np.log(q_est))
    initial[list(S)] = (1 - 2 * e[list(S)].astype(float)) * 100
    initial[list(P)] = 0
    states = PaperStates(H, s, initial)
    history = [float(np.mean(np.abs(initial[mask])))] if mask.any() else []
    previous = deque(history, maxlen=5)
    k = 0
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("Computation deadline reached; session aborted")
        k += 1
        llrs = states.step(k)
        z = (llrs < 0).astype(np.uint8)
        if np.array_equal(np.asarray(H @ z).ravel() % 2, s):
            if mask.any():
                history.append(float(np.mean(np.abs(llrs[mask]))))
            return Result(z, None, k, history, states.backend)
        if not mask.any():
            raise ValueError("All positions are known but their syndrome is inconsistent")
        average = float(np.mean(np.abs(llrs[mask])))
        history.append(average)
        if len(previous) == 5 and average <= sum(previous) / 5:
            return Result(None, np.argsort(np.abs(llrs), kind="stable")[:d], k, history, states.backend)
        previous.append(average)


def decode(s, e, H, q_est, S, P, d):
    result = decode_result(s, e, H, q_est, S, P, d)
    return result.error, result.positions
