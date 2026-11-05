import hashlib
import json
import math
import secrets
import time
from dataclasses import dataclass, field

import numpy as np

from bp import bits, decode_result, matrix


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def entropy(q):
    if not 0 < q < 0.5:
        raise ValueError("QBER must be between 0 and 0.5")
    return -q * math.log2(q) - (1 - q) * math.log2(1 - q)


def untainted(H, seed=0):
    rng = np.random.default_rng(seed)
    occupied, positions = set(), []
    for col in rng.permutation(H.shape[1]):
        rows = set(H[:, int(col)].nonzero()[0].tolist())
        if rows and not occupied.intersection(rows):
            positions.append(int(col))
            occupied.update(rows)
    return positions


def make_pool(n=96):
    if n not in (96, 192, 384):
        raise ValueError("Demo block length must be 96, 192 or 384")
    pool = []
    for degree in (6, 8, 12):
        rng = np.random.default_rng(2701 + degree + n)
        base = np.zeros((n // degree, n), dtype=np.uint8)
        for row in range(n // degree):
            base[row, row * degree:(row + 1) * degree] = 1
        H = np.vstack([base[:, rng.permutation(n)] for _ in range(3)])
        pool.append({"name": f"Gallager {n} / {H.shape[0]}", "H": H.tolist()})
    return pool


def choose_spec(pool, q_est, alpha=1.0, seed=7):
    h = entropy(q_est)
    if not 0 < alpha <= 2:
        raise ValueError("alpha must be in (0, 2]")
    options = []
    for entry in pool:
        H = matrix(entry["H"])
        m, n = H.shape
        if not 0 < m < n or n > 4096:
            raise ValueError("Code dimensions must satisfy 0 < m < n <= 4096")
        if m / (n * h) > 1:
            p, s = math.floor((m - n * h) / (1 - h)), 0
        elif m / (n * h) < 1:
            p, s = 0, math.ceil(n - m / h)
        else:
            p, s = 0, 0
        if not 0 <= p + s < n:
            continue
        rng = np.random.default_rng(seed)
        candidates = untainted(H, seed)
        P = candidates[:p] if len(candidates) >= p else [int(i) for i in rng.permutation(n)[:p]]
        S = [int(i) for i in rng.permutation(n)[:s]]
        d = math.ceil(n * (0.0280 - 0.02 * (1 - m / n)) * alpha)
        options.append({"name": entry.get("name", "Imported code"), "H": H.toarray().tolist(), "q_est": q_est, "S": sorted(S), "P": sorted(P), "d": d, "verification_bits": 64})
    if not options:
        raise ValueError("No code can hold a raw-key block")
    return max(options, key=lambda x: len(x["H"][0]) - len(x["S"]) - len(x["P"]))


def validate_spec(spec):
    allowed = {"name", "H", "q_est", "S", "P", "d", "verification_bits"}
    if set(spec) != allowed:
        raise ValueError("Unexpected protocol configuration fields")
    H = matrix(spec["H"])
    m, n = H.shape
    S, P = spec["S"], spec["P"]
    if not 0 < m < n <= 4096 or not 0 < spec["q_est"] < 0.5:
        raise ValueError("Invalid code dimensions or estimated QBER")
    if len(S) != len(set(S)) or len(P) != len(set(P)) or set(S) & set(P):
        raise ValueError("Invalid position sets")
    if any(type(i) is not int or not 0 <= i < n for i in S + P):
        raise ValueError("Invalid position")
    if not 0 <= len(S) + len(P) < n or type(spec["d"]) is not int or not 1 <= spec["d"] <= n:
        raise ValueError("Invalid disclosure size")
    if spec["verification_bits"] != 64:
        raise ValueError("Verification uses 64 bits")
    return H


def random_bits(n):
    return np.fromiter((secrets.randbits(1) for _ in range(n)), dtype=np.uint8)


def toeplitz(key, seed, size=64):
    key = np.asarray(key, dtype=np.uint8)
    seed = bits(seed, len(key) + size - 1)
    return [int(np.dot(seed[len(key) - 1 + row - np.arange(len(key))], key) % 2) for row in range(size)]


@dataclass
class Party:
    role: str
    session_id: str
    spec: dict
    raw: object
    punctures: object = None
    H: object = field(init=False)
    rounds: list = field(default_factory=list)

    def __post_init__(self):
        self.H = validate_spec(self.spec)
        n = self.H.shape[1]
        self.S, self.P = set(self.spec["S"]), set(self.spec["P"])
        self.raw_positions = np.array([i for i in range(n) if i not in self.S | self.P])
        self.raw = bits(self.raw, len(self.raw_positions))
        self.extended = np.zeros(n, dtype=np.uint8)
        self.extended[self.raw_positions] = self.raw
        positions = sorted(self.P)
        values = random_bits(len(positions)) if self.punctures is None else bits(self.punctures, len(positions))
        self.extended[positions] = values
        self.syndrome = (np.asarray(self.H @ self.extended).ravel() % 2).tolist()
        self.error = np.zeros(n, dtype=np.uint8)
        self.relative = None
        self.outcome = None
        self.disclosed = 0
        self.created = time.monotonic()
        self.status = "prepared"
        self.corrected = None

    def connect(self, peer_syndrome):
        self.relative = np.asarray(self.syndrome, dtype=np.uint8) ^ bits(peer_syndrome, self.H.shape[0])
        return self.attempt()

    def attempt(self):
        result = decode_result(self.relative, self.error, self.H, self.spec["q_est"], sorted(self.S), sorted(self.P), self.spec["d"], time.monotonic() + 20)
        converged = result.error is not None
        positions = [] if converged else [int(i) for i in result.positions]
        self.outcome = {"converged": converged, "positions": positions, "error_digest": digest(result.error.tolist()) if converged else None}
        self.rounds.append({"round": len(self.rounds), "iterations": result.iterations, "confidence": [float(x) if np.isfinite(x) else None for x in result.confidence], "backend": result.backend, "positions": positions, "converged": converged})
        if converged:
            self.corrected = (self.extended ^ result.error)[self.raw_positions] if self.role == "bob" else self.raw.copy()
            self.status = "decoded"
        else:
            self.status = "disclosure required"
        return self.outcome

    def reveal(self):
        if self.outcome is None or self.outcome["converged"]:
            raise ValueError("No disclosure is pending")
        D = self.outcome["positions"]
        if not set(D) - self.S:
            raise ValueError("Paper disclosure set contains no new positions; session aborted")
        return D, self.extended[D].tolist()

    def update(self, positions, peer_values):
        D, _ = self.reveal()
        if positions != D:
            raise ValueError("Disclosure positions disagree")
        self.error[D] = self.extended[D] ^ bits(peer_values, len(D))
        self.S.update(D)
        self.P.difference_update(D)
        self.disclosed += len(D)
        return self.attempt()

    def verify(self, seed, tag):
        if self.corrected is None:
            raise ValueError("Decode before verification")
        valid = secrets.compare_digest(bytes(toeplitz(self.corrected, seed)), bytes(bits(tag, 64)))
        self.status = "verified" if valid else "verification failed"
        return valid

    def summary(self):
        m, n = self.H.shape
        P0 = self.spec["P"]
        punctured_checks = np.asarray(self.H[:, P0].sum(axis=1)).ravel() if P0 else np.zeros(m)
        applicable = bool(np.all(punctured_checks <= 1) and all(self.H[:, i].nnz for i in P0))
        leakage = m - len(P0) + self.disclosed
        return {"role": self.role, "status": self.status, "n": n, "m": m, "raw_bits": len(self.raw), "initial_punctures": len(P0), "initial_shortened": len(self.spec["S"]), "additional_rounds": max(0, len(self.rounds) - 1), "disclosed_per_party": self.disclosed, "paper_leakage": leakage if applicable else None, "paper_bound_applicable": applicable, "conservative_public_bits": 2 * (m + self.disclosed) + 64, "efficiency": leakage / (len(self.raw) * entropy(self.spec["q_est"])), "rounds": self.rounds}


def agree(left, right):
    if left != right:
        raise ValueError("Alice and Bob produced different BP results")


def simulate(spec, alice_raw, bob_raw, punctures=None):
    sid = secrets.token_hex(16)
    a = Party("alice", sid, spec, alice_raw, None if punctures is None else punctures[0])
    b = Party("bob", sid, spec, bob_raw, None if punctures is None else punctures[1])
    transcript = [{"kind": "syndromes", "round": 0, "alice_bits": len(a.syndrome), "bob_bits": len(b.syndrome)}]
    agree(a.connect(b.syndrome), b.connect(a.syndrome))
    while not a.outcome["converged"]:
        D, av = a.reveal()
        _, bv = b.reveal()
        agree(a.update(D, bv), b.update(D, av))
        transcript.append({"kind": "disclosure", "round": len(a.rounds) - 1, "positions": D, "alice_bits": len(D), "bob_bits": len(D)})
    seed = random_bits(len(a.raw) + 63).tolist()
    tag = toeplitz(a.raw, seed)
    verified = b.verify(seed, tag)
    a.status = "verified" if verified else "verification failed"
    transcript.append({"kind": "verification", "round": len(a.rounds), "tag_bits": 64, "matched": verified})
    return a, b, transcript
