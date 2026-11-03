#!/usr/bin/env python3
"""Check a real image's first TLS handshake while HTTP configuration is delayed.

Run on the Docker daemon host. All listeners bind to loopback, credentials are
temporary fixture material, and normal certificate/hostname verification stays on.
"""

import argparse
import concurrent.futures
import http.server
import json
import os
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path


def command(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True).stdout.strip()


def make_certificates(root):
    command("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(root / "ca.key"), "-out", str(root / "ca.pem"),
            "-days", "1", "-subj", "/CN=Traefik startup fixture CA")
    command("openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(root / "leaf.key"), "-out", str(root / "leaf.csr"),
            "-subj", "/CN=startup.test")
    (root / "leaf.ext").write_text(
        "subjectAltName=DNS:startup.test\nbasicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n")
    command("openssl", "x509", "-req", "-in", str(root / "leaf.csr"),
            "-CA", str(root / "ca.pem"), "-CAkey", str(root / "ca.key"),
            "-CAcreateserial", "-out", str(root / "leaf.pem"), "-days", "1",
            "-sha256", "-extfile", str(root / "leaf.ext"))


class ConfigurationHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requested.set()
        if not self.server.release.wait(self.server.deadline):
            self.send_error(503)
            return
        config = {
            "http": {
                "routers": {"fixture": {"entryPoints": ["secure"],
                    "rule": "Host(`startup.test`)", "service": "fixture", "tls": {}}},
                "services": {"fixture": {"loadBalancer": {
                    "servers": [{"url": "http://127.0.0.1:9"}]}}},
            },
            "tls": {"certificates": [{"certFile": "/fixture/leaf.pem",
                                      "keyFile": "/fixture/leaf.key"}]},
        }
        body = json.dumps(config).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def handshake(port, ca_file, deadline):
    context = ssl.create_default_context(cafile=str(ca_file))
    try:
        with (socket.create_connection(("127.0.0.1", port), timeout=deadline) as raw,
              context.wrap_socket(raw, server_hostname="startup.test") as tls):
            return {"verified": True, "protocol": tls.version()}
    except ssl.SSLCertVerificationError as error:
        return {"verified": False, "error_class": type(error).__name__,
                "verification_code": error.verify_code}
    except ssl.SSLError as error:
        return {"verified": False, "error_class": type(error).__name__}


def unused_loopback_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def start_container(image, root, port, http_port, name):
    return command("docker", "run", "--detach", "--rm", "--name", name,
        "--network", "host", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--mount",
        f"type=bind,src={root},dst=/fixture,readonly", image,
        f"--entryPoints.secure.address=127.0.0.1:{port}",
        f"--providers.http.endpoint=http://127.0.0.1:{http_port}/configuration",
        "--global.checkNewVersion=false", "--global.sendAnonymousUsage=false",
        "--log.level=ERROR")


def verify(image, hold_seconds, deadline):
    name = "traefik-startup-fixture-" + uuid.uuid4().hex[:12]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ConfigurationHandler)
    server.daemon_threads = True
    server.requested, server.release = threading.Event(), threading.Event()
    server.deadline = deadline
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    started = time.monotonic()
    container_started = False
    temporary = tempfile.TemporaryDirectory(prefix="traefik-startup-fixture-")
    try:
        directory = temporary.name
        root = Path(directory)
        make_certificates(root)
        port = unused_loopback_port()
        start_container(image, root, port, server.server_port, name)
        container_started = True
        if not server.requested.wait(deadline):
            raise RuntimeError("HTTP provider did not request the controlled fixture")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(handshake, port, root / "ca.pem", deadline)
            try:
                early = pending.result(timeout=hold_seconds)
            except concurrent.futures.TimeoutError:
                early = None
            server.release.set()
            final = pending.result(timeout=deadline)
        return {"image": image, "provider_request_observed": True,
                "early_handshake": early, "first_handshake": final,
                "startup_tls_ready": early is None and final["verified"],
                "elapsed_seconds": round(time.monotonic() - started, 3)}
    finally:
        server.release.set()
        if container_started:
            subprocess.run(["docker", "rm", "--force", name], check=False,
                           capture_output=True, text=True)
        server.shutdown()
        server.server_close()
        thread.join()
        temporary.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--hold-seconds", type=float, default=3.0)
    parser.add_argument("--deadline", type=float, default=30.0)
    parser.add_argument("--expect", choices=("ready", "early-failure"), default="ready")
    args = parser.parse_args()
    if not 0 < args.hold_seconds < args.deadline:
        parser.error("the finite fixture observation must precede its deadline")
    os.umask(0o077)
    result = verify(args.image, args.hold_seconds, args.deadline)
    print(json.dumps(result, sort_keys=True), flush=True)
    if args.expect == "early-failure":
        early = result["early_handshake"] or {}
        return 0 if early and early.get("verified") is False else 1
    return 0 if result["startup_tls_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
