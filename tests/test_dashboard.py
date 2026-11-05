import http.client
import json
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard import DashboardHandler


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        conn.request(method, path, body, {"Content-Type": "application/json"})
        response = conn.getresponse()
        result = response.status, response.read()
        conn.close()
        return result

    def test_page_and_assets(self):
        for path in ("/", "/styles.css", "/app.js", "/api", "/api/index", "/api/index.py"):
            status, content = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertTrue(content)

    def test_simulation_api(self):
        status, content = self.request("POST", "/api", "{}")
        self.assertEqual(status, 200)
        result = json.loads(content)
        self.assertTrue(result["verified"])
        self.assertEqual(result["transport"], "In-process simulation")

    def test_initial_parameter_preview(self):
        status, content = self.request("POST", "/api", json.dumps({"action": "parameters", "n": 96, "q_est": .05}))
        self.assertEqual(status, 200)
        plan = json.loads(content)
        self.assertEqual(plan["raw_bits"] + plan["initial_punctures"] + plan["initial_shortened"], 96)
        self.assertGreater(plan["d"], 0)

    def test_initial_parameter_errors(self):
        status, content = self.request("POST", "/api", json.dumps({"action": "parameters", "q_est": 0}))
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(content))

    def test_interface_does_not_name_the_paper(self):
        _, content = self.request("GET", "/")
        text = content.decode().lower()
        for name in ("kiktenko", "arxiv", "appendix", "research paper", "the paper"):
            self.assertNotIn(name, text)

    def test_malformed_json_and_unknown_route(self):
        self.assertEqual(self.request("POST", "/api", "{")[0], 400)
        self.assertEqual(self.request("GET", "/missing")[0], 404)
