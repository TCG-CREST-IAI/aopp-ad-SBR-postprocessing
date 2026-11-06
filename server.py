import argparse
import copy
import hashlib
import json
import os
import re
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from protocol import Party, agree, digest, random_bits, toeplitz
from transport import Client, fingerprint, load_config


class Service:
    def __init__(self, role, config):
        self.role, self.config = role, config
        self.sessions, self.lock = {}, threading.RLock()
        self.pipeline = None
        self.peer = Client(config["peer_url"], config["ca"], config["cert"], config["key"], config["peer_cert"])
        self.identities = {fingerprint(config["peer_cert"]): "peer", fingerprint(config["operator_cert"]): "operator"}

    def execute(self, path, payload, identity):
        if not isinstance(payload, dict):
            raise ValueError("Expected a JSON object")
        if path == "/pipeline":
            with self.lock:
                if self.pipeline is None:
                    from pipeline_service import Pipeline
                    self.pipeline = Pipeline(self)
            return self.pipeline.dispatch(payload, identity)
        with self.lock:
            now = time.monotonic()
            for sid in list(self.sessions):
                if now - self.sessions[sid]["party"].created > 900:
                    del self.sessions[sid]
            if path == "/health":
                return {"role": self.role, "transport": "mTLS", "protocol": "symmetric-blind-v1"}
            if path == "/exchange":
                if identity != "peer" or self.role != "bob":
                    raise PermissionError("Only Alice's pinned certificate can exchange with Bob")
            elif identity != "operator":
                raise PermissionError("This operation requires the pinned operator certificate")
            sid = payload.get("session_id", "")
            if not isinstance(sid, str) or not re.fullmatch(r"[a-f0-9]{32}", sid):
                raise ValueError("session_id must contain 32 lowercase hexadecimal characters")
            if path == "/prepare":
                if sid in self.sessions:
                    if self.sessions[sid]["prepare_digest"] != digest(payload):
                        raise ValueError("Session already exists with different input")
                    return {"prepared": True, "role": self.role}
                if len(self.sessions) >= 16:
                    raise ValueError("Session capacity reached")
                party = Party(self.role, sid, payload["spec"], payload["raw_key"])
                self.sessions[sid] = {"party": party, "prepare_digest": digest(payload), "cache": {}, "transcript": []}
                return {"prepared": True, "role": self.role}
            if sid not in self.sessions:
                raise ValueError("Unknown or expired session")
            record = self.sessions[sid]
            party = record["party"]
            if path == "/delete":
                del self.sessions[sid]
                return {"deleted": True}
            if path == "/result":
                return {"summary": party.summary(), "transcript": record["transcript"]}
            if path == "/run":
                if self.role != "alice":
                    raise ValueError("Start reconciliation on Alice")
                return self.run_alice(record)
            if path == "/exchange":
                return self.exchange(record, payload)
            raise ValueError("Unknown operation")

    def exchange(self, record, payload):
        before = copy.deepcopy(record["party"])
        try:
            return self.exchange_message(record, payload)
        except Exception:
            record["party"] = before
            raise

    def exchange_message(self, record, payload):
        party = record["party"]
        if payload.get("spec_digest") != digest(party.spec):
            raise ValueError("Protocol configurations differ")
        sequence = payload.get("sequence")
        if type(sequence) is not int or sequence < 0:
            raise ValueError("Invalid sequence number")
        request_digest = digest(payload)
        if sequence in record["cache"]:
            previous, response = record["cache"][sequence]
            if previous != request_digest:
                raise ValueError("A sequence number was reused with a different payload")
            return response
        if sequence != len(record["cache"]):
            raise ValueError("Out-of-order protocol message")
        kind = payload.get("kind")
        if kind == "syndromes" and sequence == 0:
            outcome = party.connect(payload["syndrome"])
            response = {"syndrome": party.syndrome, "outcome": outcome}
        elif kind == "disclosure" and party.status == "disclosure required":
            positions, values = party.reveal()
            if payload["positions"] != positions:
                raise ValueError("Disclosure sets differ")
            outcome = party.update(positions, payload["values"])
            response = {"values": values, "outcome": outcome}
        elif kind == "verification" and party.status == "decoded":
            response = {"verified": party.verify(payload["seed"], payload["tag"])}
            if response["verified"] and self.config.get("output_dir") and not record.get("defer_output"):
                directory = Path(self.config["output_dir"])
                directory.mkdir(parents=True, exist_ok=True)
                target = directory / f"{party.session_id}.bits"
                with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
                    stream.write("".join(str(int(bit)) for bit in party.corrected))
        else:
            raise ValueError("Message does not match the session state")
        record["cache"][sequence] = request_digest, response
        return response

    def run_alice(self, record):
        party = record["party"]
        if party.status in ("verified", "verification failed"):
            return {"summary": party.summary(), "transcript": record["transcript"]}
        if party.relative is None:
            response = self.peer.post("/exchange", {"session_id": party.session_id, "spec_digest": digest(party.spec), "sequence": 0, "kind": "syndromes", "syndrome": party.syndrome})
            agree(party.connect(response["syndrome"]), response["outcome"])
            record["transcript"].append({"kind": "syndromes", "round": 0, "alice_bits": len(party.syndrome), "bob_bits": len(response["syndrome"])})
        deadline = time.monotonic() + 120
        while not party.outcome["converged"]:
            if time.monotonic() > deadline or len(party.rounds) > party.H.shape[1] + 1:
                raise TimeoutError("SBI attempt exceeded its time/round budget; reject this attempt")
            positions, values = party.reveal()
            sequence = len(party.rounds)
            payload = {"session_id": party.session_id, "spec_digest": digest(party.spec), "sequence": sequence, "kind": "disclosure", "positions": positions, "values": values}
            response = self.peer.post("/exchange", payload)
            agree(party.update(positions, response["values"]), response["outcome"])
            record["transcript"].append({"kind": "disclosure", "round": sequence, "positions": positions, "alice_bits": len(positions), "bob_bits": len(positions)})
        if "verification" not in record:
            seed = random_bits(len(party.raw) + 63).tolist()
            record["verification"] = {"session_id": party.session_id, "spec_digest": digest(party.spec), "sequence": len(party.rounds), "kind": "verification", "seed": seed, "tag": toeplitz(party.raw, seed)}
        response = self.peer.post("/exchange", record["verification"])
        if type(response.get("verified")) is not bool:
            raise ValueError("Invalid verification acknowledgment")
        party.status = "verified" if response["verified"] else "verification failed"
        record["transcript"].append({"kind": "verification", "round": len(party.rounds), "tag_bits": 64, "matched": response["verified"]})
        return {"summary": party.summary(), "transcript": record["transcript"]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        try:
            self.connection.settimeout(120)
            cert = self.connection.getpeercert(binary_form=True)
            identity = self.server.service.identities.get(hashlib.sha256(cert).hexdigest())
            if identity is None:
                raise PermissionError("Certificate is not an authorized identity")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2_000_000:
                raise ValueError("Invalid request size")
            payload = json.loads(self.rfile.read(length))
            result = self.server.service.execute(self.path, payload, identity)
            status = 200
        except PermissionError as exc:
            status, result = 403, {"error": str(exc)}
        except (ValueError, KeyError, TypeError, TimeoutError, FloatingPointError, OSError) as exc:
            status, result = 400, {"error": str(exc)}
        except Exception:
            status, result = 500, {"error": "Internal protocol error"}
        data = json.dumps(result, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def build_server(role, config):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(config["ca"])
    if config.get("crl"):
        context.load_verify_locations(config["crl"])
        context.verify_flags |= ssl.VERIFY_CRL_CHECK_LEAF
    context.load_cert_chain(config["cert"], config["key"])
    class TLSServer(ThreadingHTTPServer):
        def get_request(self):
            raw, address = super().get_request()
            raw.settimeout(10)
            try:
                return context.wrap_socket(raw, server_side=True), address
            except Exception:
                raw.close()
                raise
    server = TLSServer((config.get("host", "127.0.0.1"), config["port"]), Handler)
    server.daemon_threads = True
    server.service = Service(role, config)
    return server


def main(role):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    config = load_config(parser.parse_args().config)
    server = build_server(role, config)
    print(f"{role.title()} listening with required mTLS on port {server.server_port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
