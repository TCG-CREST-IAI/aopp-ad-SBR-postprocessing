import argparse
from http.server import ThreadingHTTPServer

from dashboard import DashboardHandler


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Demonstration dashboard: http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
