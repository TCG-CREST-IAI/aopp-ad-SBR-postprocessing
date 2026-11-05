import argparse
import json
import os
import secrets
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from protocol import choose_spec, make_pool


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="private/inputs")
    parser.add_argument("--n", type=int, default=96)
    parser.add_argument("--q", type=float, default=.05)
    parser.add_argument("--q-est", type=float, default=.05)
    parser.add_argument("--alpha", type=float, default=1)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--pool")
    args = parser.parse_args()
    if not 0 <= args.q < .5:
        parser.error("q must be between 0 and 0.5")
    pool = json.loads(Path(args.pool).read_text()) if args.pool else make_pool(args.n)
    spec = choose_spec(pool, args.q_est, args.alpha, args.seed)
    n = len(spec["H"][0]) - len(spec["S"]) - len(spec["P"])
    rng = np.random.default_rng(args.seed)
    a = rng.integers(0, 2, n, dtype=np.uint8)
    b = a ^ (rng.random(n) < args.q).astype(np.uint8)
    sid = secrets.token_hex(16)
    folder = Path(args.out)
    folder.mkdir(parents=True, exist_ok=True)
    values = {"alice.bits": "".join(map(str, a)), "bob.bits": "".join(map(str, b)), "spec.json": json.dumps(spec), "session.txt": sid}
    for name, value in values.items():
        with os.fdopen(os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
            stream.write(value)
    print(json.dumps({"session_id": sid, "raw_bits": n, "code": spec["name"], "data": "synthetic"}, indent=2))
