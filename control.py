import argparse
import json
from pathlib import Path

from transport import Client, load_config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["prepare", "run", "result", "delete"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--party", choices=["alice", "bob"], required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--spec")
    parser.add_argument("--key-file")
    args = parser.parse_args()
    config = load_config(args.config)
    client = Client(config[f"{args.party}_url"], config["ca"], config["cert"], config["key"], config[f"{args.party}_cert"])
    payload = {"session_id": args.session}
    if args.operation == "prepare":
        if not args.spec or not args.key_file:
            parser.error("prepare requires --spec and --key-file")
        payload["spec"] = json.loads(Path(args.spec).read_text())
        raw = Path(args.key_file).read_text().strip()
        if set(raw) - {"0", "1"}:
            raise ValueError("Key files must contain only a bit string")
        payload["raw_key"] = [int(bit) for bit in raw]
    print(json.dumps(client.post(f"/{args.operation}", payload), indent=2))


if __name__ == "__main__":
    main()
