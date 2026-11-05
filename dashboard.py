import json
import os
import secrets
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import numpy as np

from protocol import choose_spec, make_pool, simulate
from transport import Client, load_config


ROOT = Path(__file__).resolve().parent


def preview_parameters(payload):
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    n = int(payload.get("n", 96))
    q_est = float(payload.get("q_est", .05))
    alpha = float(payload.get("alpha", 1))
    seed = int(payload.get("seed", 17))
    if not 0 <= float(payload.get("q", .05)) <= .25 or not 0 <= seed < 2**32:
        raise ValueError("Invalid channel parameters")
    spec = choose_spec(make_pool(n), q_est, alpha, seed)
    return {"code": spec["name"], "n": n, "m": len(spec["H"]), "raw_bits": n - len(spec["S"]) - len(spec["P"]), "initial_punctures": len(spec["P"]), "initial_shortened": len(spec["S"]), "d": spec["d"]}


def run_demo(payload):
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    started = time.perf_counter()
    n = int(payload.get("n", 96))
    q = float(payload.get("q", 0.05))
    q_est = float(payload.get("q_est", 0.05))
    alpha = float(payload.get("alpha", 1))
    seed = int(payload.get("seed", 17))
    mode = payload.get("mode", "simulation")
    if not 0 <= q <= 0.25 or not 0 <= seed < 2**32 or mode not in ("simulation", "mtls"):
        raise ValueError("Invalid demonstration parameters")
    spec = choose_spec(make_pool(n), q_est, alpha, seed)
    length = n - len(spec["S"]) - len(spec["P"])
    rng = np.random.default_rng(seed)
    a_raw = rng.integers(0, 2, length, dtype=np.uint8)
    noise = (rng.random(length) < q).astype(np.uint8)
    b_raw = a_raw ^ noise
    if mode == "simulation":
        a, b, transcript = simulate(spec, a_raw, b_raw)
        alice, bob = a.summary(), b.summary()
        exact_match = bool(np.array_equal(a_raw, b.corrected))
    else:
        filename = os.environ.get("OPERATOR_CONFIG")
        if not filename:
            raise ValueError("Live mTLS is not configured on this dashboard server")
        token = os.environ.get("DEMO_TOKEN", "")
        if not token or not secrets.compare_digest(payload.get("token", ""), token):
            raise PermissionError("A valid operator token is required for live tests")
        config = load_config(filename)
        clients = {role: Client(config[f"{role}_url"], config["ca"], config["cert"], config["key"], config[f"{role}_cert"]) for role in ("alice", "bob")}
        sid = secrets.token_hex(16)
        prepared = []
        try:
            for role, raw in (("alice", a_raw), ("bob", b_raw)):
                clients[role].post("/prepare", {"session_id": sid, "spec": spec, "raw_key": raw.tolist()})
                prepared.append(role)
            result = clients["alice"].post("/run", {"session_id": sid})
            alice, transcript = result["summary"], result["transcript"]
            bob = clients["bob"].post("/result", {"session_id": sid})["summary"]
            exact_match = None
        finally:
            for role in prepared:
                try:
                    clients[role].post("/delete", {"session_id": sid})
                except (ValueError, OSError):
                    pass
    return {"mode": mode, "transport": "mTLS over HTTPS" if mode == "mtls" else "In-process simulation", "elapsed_ms": round((time.perf_counter() - started) * 1000, 1), "code": spec["name"], "q_est": q_est, "observed_qber": float(noise.mean()), "input_errors": int(noise.sum()), "d": spec["d"], "alice": alice, "bob": bob, "transcript": transcript, "verified": bob["status"] == "verified", "exact_match": exact_match, "demo_alice": a_raw[:96].tolist(), "demo_bob": b_raw[:96].tolist()}


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_headers(self, status, content_type, length):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()

    def do_GET(self):
        if self.path.split("?")[0] in ("/api", "/api/index", "/api/index.py"):
            data = json.dumps({"live_available": bool(os.environ.get("OPERATOR_CONFIG")), "protocol": "Symmetric blind reconciliation"}).encode()
            self.send_headers(200, "application/json", len(data))
            self.wfile.write(data)
            return
        paths = {"/": ("index.html", "text/html; charset=utf-8"), "/styles.css": ("styles.css", "text/css"), "/app.js": ("app.js", "application/javascript")}
        item = paths.get(self.path.split("?")[0])
        if item is None:
            self.send_error(404)
            return
        data = (ROOT / "web" / item[0]).read_bytes()
        self.send_headers(200, item[1], len(data))
        self.wfile.write(data)

    def do_POST(self):
        try:
            if self.path.split("?")[0] not in ("/api", "/api/index", "/api/index.py"):
                raise ValueError("Unknown endpoint")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192:
                raise ValueError("Invalid request size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
            result = preview_parameters(payload) if payload.get("action") == "parameters" else run_demo(payload)
            status = 200
        except PermissionError as exc:
            status, result = 403, {"error": str(exc)}
        except (ValueError, KeyError, TypeError, TimeoutError, FloatingPointError, OSError) as exc:
            status, result = 400, {"error": str(exc)}
        except Exception:
            status, result = 500, {"error": "The demonstration could not complete"}
        data = json.dumps(result, allow_nan=False).encode()
        self.send_headers(status, "application/json", len(data))
        self.wfile.write(data)
