import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard import run_demo
from tools.create_certs import create
from transport import Client, load_config


def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class ProcessTests(unittest.TestCase):
    def test_live_gateway_with_two_separate_python_processes(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / "private"
            create(private, alice_port=port(), bob_port=port())
            config = load_config(private / "operator" / "config.json")
            processes = []
            try:
                for role in ("bob", "alice"):
                    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
                    process = subprocess.Popen([sys.executable, str(root / f"{role}.py"), "--config", str(private / role / "config.json")], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
                    processes.append(process)
                    client = Client(config[f"{role}_url"], config["ca"], config["cert"], config["key"], config[f"{role}_cert"], timeout=1)
                    deadline = time.monotonic() + 20
                    while True:
                        if process.poll() is not None:
                            self.fail(process.stderr.read().decode())
                        try:
                            self.assertEqual(client.post("/health", {})["transport"], "mTLS")
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise TimeoutError("Party process did not become ready")
                            time.sleep(.1)
                token = secrets.token_urlsafe(32)
                with patch.dict(os.environ, {"OPERATOR_CONFIG": str(private / "operator" / "config.json"), "DEMO_TOKEN": token}):
                    with self.assertRaises(PermissionError):
                        run_demo({"mode": "mtls", "token": "wrong"})
                    result = run_demo({"mode": "mtls", "token": token})
                self.assertTrue(result["verified"])
                self.assertEqual(result["transport"], "mTLS over HTTPS")
                self.assertNotEqual(processes[0].pid, processes[1].pid)
                output = list((private / "bob" / "output").glob("*.bits"))
                self.assertEqual(len(output), 1)
                self.assertEqual(output[0].read_text(), "".join(str(bit) for bit in result["demo_alice"]))
            finally:
                for process in processes:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    process.stdout.close()
                    process.stderr.close()
