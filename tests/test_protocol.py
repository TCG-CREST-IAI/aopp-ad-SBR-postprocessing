import http.client
import json
import os
import secrets
import socket
import ssl
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard import run_demo
from protocol import Party, choose_spec, digest, make_pool, simulate, toeplitz, untainted
from server import build_server
from tools.create_certs import create
from transport import Client, load_config


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class ProtocolTests(unittest.TestCase):
    def test_code_selection_and_padding(self):
        for q in (.02, .05, .15, .24):
            spec = choose_spec(make_pool(96), q)
            self.assertFalse(spec["S"] and spec["P"])
            party = Party("alice", secrets.token_hex(16), spec, [0] * (96 - len(spec["S"]) - len(spec["P"])))
            self.assertTrue(np.all(party.extended[spec["S"]] == 0))

    def test_simulated_end_to_end(self):
        for n in (96, 192, 384):
            for seed in (1, 17, 83):
                result = run_demo({"n": n, "seed": seed})
                self.assertTrue(result["verified"])
                self.assertTrue(result["exact_match"])
                self.assertEqual(result["alice"]["rounds"], result["bob"]["rounds"])

    def test_verification_rejects_wrong_candidate(self):
        seed = np.random.default_rng(7).integers(0, 2, 79).tolist()
        original = np.zeros(16, dtype=np.uint8)
        corrupted = original.copy()
        corrupted[0] = 1
        self.assertNotEqual(toeplitz(original, seed), toeplitz(corrupted, seed))

    def test_invalid_inputs(self):
        for payload in ({"n": 999}, {"q_est": 0}, {"q": .7}, {"mode": "plaintext"}):
            with self.assertRaises(ValueError):
                run_demo(payload)


class MutualTLSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        cls.root = root
        alice_port, bob_port = free_port(), free_port()
        create(root / "private", alice_port=alice_port, bob_port=bob_port)
        cls.configs = {role: load_config(root / "private" / role / "config.json") for role in ("alice", "bob", "operator")}
        cls.servers = [build_server(role, cls.configs[role]) for role in ("alice", "bob")]
        cls.threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in cls.servers]
        for thread in cls.threads:
            thread.start()
        config = cls.configs["operator"]
        cls.clients = {role: Client(config[f"{role}_url"], config["ca"], config["cert"], config["key"], config[f"{role}_cert"]) for role in ("alice", "bob")}

    @classmethod
    def tearDownClass(cls):
        for server in cls.servers:
            server.shutdown()
            server.server_close()
        for thread in cls.threads:
            thread.join(timeout=5)
        cls.temp.cleanup()

    def prepare(self):
        sid = secrets.token_hex(16)
        spec = choose_spec(make_pool(96), .05)
        length = 96 - len(spec["S"]) - len(spec["P"])
        a = np.random.default_rng(17).integers(0, 2, length, dtype=np.uint8)
        b = a.copy()
        b[::17] ^= 1
        for role, raw in (("alice", a), ("bob", b)):
            self.clients[role].post("/prepare", {"session_id": sid, "spec": spec, "raw_key": raw.tolist()})
        return sid, spec, a

    def test_real_mtls_end_to_end_and_local_output(self):
        sid, _, a = self.prepare()
        result = self.clients["alice"].post("/run", {"session_id": sid})
        self.assertEqual(result["summary"]["status"], "verified")
        bob = self.clients["bob"].post("/result", {"session_id": sid})
        self.assertEqual(bob["summary"]["status"], "verified")
        output = Path(self.configs["bob"]["output_dir"]) / f"{sid}.bits"
        self.assertEqual(output.read_text(), "".join(str(int(bit)) for bit in a))
        self.assertNotIn("raw_key", json.dumps(result))
        self.assertEqual(result, self.clients["alice"].post("/run", {"session_id": sid}))

    def test_connection_without_client_certificate_is_rejected(self):
        config = self.configs["operator"]
        context = ssl.create_default_context(cafile=config["ca"])
        port = self.servers[1].server_port
        connection = http.client.HTTPSConnection("localhost", port, context=context, timeout=3)
        with self.assertRaises(OSError):
            connection.request("POST", "/health", "{}")
            connection.getresponse().read()
        connection.close()

    def test_operator_cannot_impersonate_alice(self):
        with self.assertRaisesRegex(ValueError, "Only Alice"):
            self.clients["bob"].post("/exchange", {})

    def test_pin_mismatch_is_rejected(self):
        config = self.configs["operator"]
        client = Client(config["bob_url"], config["ca"], config["cert"], config["key"], config["alice_cert"])
        with self.assertRaises(ssl.SSLError):
            client.post("/health", {})

    def test_untrusted_ca_is_rejected(self):
        create(self.root / "rogue")
        config = self.configs["operator"]
        rogue = load_config(self.root / "rogue" / "operator" / "config.json")
        client = Client(config["bob_url"], config["ca"], rogue["cert"], rogue["key"], config["bob_cert"], timeout=3)
        with self.assertRaises(OSError):
            client.post("/health", {})

    def test_duplicate_and_changed_protocol_messages(self):
        sid, spec, _ = self.prepare()
        alice = self.servers[0].service.sessions[sid]["party"]
        payload = {"session_id": sid, "spec_digest": digest(spec), "sequence": 0, "kind": "syndromes", "syndrome": alice.syndrome}
        client = self.servers[0].service.peer
        result = client.post("/exchange", payload)
        self.assertEqual(result, client.post("/exchange", payload))
        payload["syndrome"][0] ^= 1
        with self.assertRaisesRegex(ValueError, "reused"):
            client.post("/exchange", payload)

    def test_configuration_mismatch_is_rejected(self):
        sid, _, _ = self.prepare()
        with self.assertRaisesRegex(ValueError, "configurations differ"):
            self.servers[0].service.peer.post("/exchange", {"session_id": sid, "spec_digest": "wrong", "sequence": 0, "kind": "syndromes", "syndrome": []})
