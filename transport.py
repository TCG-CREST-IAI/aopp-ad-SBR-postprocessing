import hashlib
import http.client
import json
import ssl
from pathlib import Path
from urllib.parse import urlsplit


def fingerprint(path):
    pem = Path(path).read_text(encoding="ascii")
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


def load_config(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text())
    for key in ("ca", "cert", "key", "peer_cert", "operator_cert", "alice_cert", "bob_cert", "output_dir"):
        if key in config:
            config[key] = str((path.parent / config[key]).resolve())
    return config


class Client:
    def __init__(self, url, ca, cert, key, expected_cert, timeout=90):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("Peer URL must be an HTTPS origin")
        self.host, self.port = parsed.hostname, parsed.port or 443
        self.expected = fingerprint(expected_cert)
        self.context = ssl.create_default_context(cafile=ca)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.load_cert_chain(cert, key)
        self.timeout = timeout

    def post(self, path, body):
        if path not in ("/prepare", "/run", "/result", "/exchange", "/delete", "/health"):
            raise ValueError("Unknown peer operation")
        connection = http.client.HTTPSConnection(self.host, self.port, context=self.context, timeout=self.timeout)
        try:
            connection.connect()
            actual = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
            if actual != self.expected:
                raise ssl.SSLError("Peer certificate does not match the configured identity")
            payload = json.dumps(body, separators=(",", ":"), allow_nan=False).encode()
            connection.request("POST", path, payload, {"Content-Type": "application/json"})
            response = connection.getresponse()
            data = json.loads(response.read(2_000_001))
            if response.status != 200:
                raise ValueError(data.get("error", f"Peer returned HTTP {response.status}"))
            return data
        finally:
            connection.close()
