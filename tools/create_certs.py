import argparse
import ipaddress
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(data)


def create(out, alice_host="localhost", bob_host="localhost", alice_port=8443, bob_port=8444):
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise ValueError("Certificate output directory must be empty")
    now = datetime.now(timezone.utc)
    root_key = ec.generate_private_key(ec.SECP256R1())
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "QKD demonstration CA")])
    root = x509.CertificateBuilder().subject_name(root_name).issuer_name(root_name).public_key(root_key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365)).add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True).add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True).sign(root_key, hashes.SHA256())
    certs, keys = {}, {}
    for role, host in (("alice", alice_host), ("bob", bob_host), ("operator", None)):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, role)])
        usages = [ExtendedKeyUsageOID.CLIENT_AUTH] if role == "operator" else [ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH]
        builder = x509.CertificateBuilder().subject_name(name).issuer_name(root_name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=30)).add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True).add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True).add_extension(x509.ExtendedKeyUsage(usages), critical=False)
        if host:
            try:
                san = x509.IPAddress(ipaddress.ip_address(host))
            except ValueError:
                san = x509.DNSName(host)
            builder = builder.add_extension(x509.SubjectAlternativeName([san]), critical=False)
        certs[role] = builder.sign(root_key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)
        keys[role] = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    ca = root.public_bytes(serialization.Encoding.PEM)
    for role, peer, host, port in (("alice", "bob", bob_host, bob_port), ("bob", "alice", alice_host, alice_port)):
        folder = out / role
        for filename, data in (("ca.pem", ca), ("cert.pem", certs[role]), ("key.pem", keys[role]), ("peer.pem", certs[peer]), ("operator.pem", certs["operator"])):
            save(folder / filename, data)
        config = {"host": "0.0.0.0", "port": alice_port if role == "alice" else bob_port, "ca": "ca.pem", "cert": "cert.pem", "key": "key.pem", "peer_cert": "peer.pem", "operator_cert": "operator.pem", "peer_url": f"https://{host}:{port}", "output_dir": "output"}
        save(folder / "config.json", json.dumps(config, indent=2).encode())
    folder = out / "operator"
    for filename, data in (("ca.pem", ca), ("cert.pem", certs["operator"]), ("key.pem", keys["operator"]), ("alice.pem", certs["alice"]), ("bob.pem", certs["bob"])):
        save(folder / filename, data)
    config = {"ca": "ca.pem", "cert": "cert.pem", "key": "key.pem", "alice_cert": "alice.pem", "bob_cert": "bob.pem", "alice_url": f"https://{alice_host}:{alice_port}", "bob_url": f"https://{bob_host}:{bob_port}"}
    save(folder / "config.json", json.dumps(config, indent=2).encode())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="private")
    parser.add_argument("--alice-host", default="localhost")
    parser.add_argument("--bob-host", default="localhost")
    parser.add_argument("--alice-port", type=int, default=8443)
    parser.add_argument("--bob-port", type=int, default=8444)
    args = parser.parse_args()
    create(args.out, args.alice_host, args.bob_host, args.alice_port, args.bob_port)
    print("Created separate Alice, Bob and operator certificate bundles")
