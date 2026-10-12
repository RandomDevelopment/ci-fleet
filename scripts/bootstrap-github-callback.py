#!/usr/bin/env python3
import argparse
import ctypes
import html
import http.server
import os
import secrets
import signal
import socket
import socketserver
import stat
import tempfile
import urllib.parse
from typing import cast


class Server(http.server.HTTPServer):
    address_family = socket.AF_UNIX
    organization: str
    state: str
    manifest: str
    output: str
    handoff: str
    done: bool
    consumed: bool

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = 0


class Callback(http.server.BaseHTTPRequestHandler):
    server_version = "ci-fleet-bootstrap"
    sys_version = ""

    @property
    def app_server(self) -> Server:
        return cast(Server, self.server)

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def reply(self, status: int, body: str) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; form-action https://github.com; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        host = self.headers.get("Host")
        if host != f"127.0.0.1:{self.app_server.server_port}" and not (self.app_server.server_port == 80 and host == "127.0.0.1"):
            self.reply(403, "Invalid callback host")
            return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/" and not parsed.query:
            action = f"https://github.com/organizations/{urllib.parse.quote(self.app_server.organization, safe='')}/settings/apps/new?state={urllib.parse.quote(self.app_server.state, safe='')}"
            body = (
                "<!doctype html><meta name=viewport content='width=device-width'>"
                "<title>Register ci-fleet GitHub App</title>"
                "<h1>Register this host's ci-fleet App</h1>"
                f"<form action='{html.escape(action, quote=True)}' method=post>"
                f"<input type=hidden name=manifest value='{html.escape(self.app_server.manifest, quote=True)}'>"
                "<button type=submit>Continue to GitHub</button></form>"
            )
            self.reply(200, body)
            return
        if parsed.path == "/next" and not parsed.query:
            try:
                target = open(self.app_server.handoff, encoding="utf-8").read().strip()
            except FileNotFoundError:
                self.reply(200, "<meta http-equiv=refresh content='1;url=/next'><p>Preparing the installation approval link…</p>")
                return
            if not target.startswith("https://github.com/apps/") or not target.endswith("/installations/new"):
                self.reply(500, "Invalid installation target")
                return
            self.reply(200, f"<h1>App created</h1><p><a href='{html.escape(target, quote=True)}'>Continue to GitHub installation approval</a></p>")
            self.app_server.done = True
            return
        if parsed.path != "/callback":
            self.reply(404, "Not found")
            return
        try:
            values = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
        except ValueError:
            self.reply(400, "Invalid callback")
            return
        if set(values) != {"code", "state"} or any(len(value) != 1 for value in values.values()):
            self.reply(400, "Invalid callback")
            return
        if not secrets.compare_digest(values["state"][0], self.app_server.state):
            self.reply(403, "Invalid callback state")
            return
        if self.app_server.consumed:
            self.reply(409, "Callback already consumed")
            return
        code = values["code"][0]
        if not code or len(code) > 512 or any(character.isspace() for character in code):
            self.reply(400, "Invalid callback")
            return
        directory = os.path.dirname(self.app_server.output)
        descriptor, temporary = tempfile.mkstemp(prefix=".manifest-code.", dir=directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(code)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, self.app_server.output)
            except FileExistsError:
                self.reply(409, "Callback already consumed")
                return
        finally:
            os.unlink(temporary)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.app_server.consumed = True
        self.reply(200, "<meta http-equiv=refresh content='1;url=/next'><h1>Registration received</h1><p>Preparing installation approval. No value needs to be copied.</p>")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--operator-uid", required=True, type=int)
    parser.add_argument("--parent-pid", required=True, type=int)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--organization", required=True)
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--handoff", required=True)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 30 <= args.timeout <= 1800:
        parser.error("invalid port or timeout")
    if os.getppid() != args.parent_pid:
        return 2
    if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM) != 0:
        raise OSError(ctypes.get_errno(), "cannot bind callback lifetime to bootstrap")
    if os.getppid() != args.parent_pid:
        return 2
    os.umask(0o077)
    descriptor = os.open(args.state_file, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            return 2
        state = handle.read()
    if not state or len(state) > 256:
        return 2
    server = Server(args.socket, Callback)
    server.server_port = args.port
    os.chmod(args.socket, 0o600)
    if args.operator_uid != os.geteuid():
        os.chown(args.socket, args.operator_uid, -1)
        # Publish operator access after the socket's ownership is final.
        os.chown(os.path.dirname(args.socket), args.operator_uid, -1)
    server.organization = args.organization
    server.state = state
    server.manifest = args.manifest
    server.output = args.output
    server.handoff = args.handoff
    server.done = False
    server.consumed = False
    server.timeout = 1
    deadline = __import__("time").monotonic() + args.timeout
    while __import__("time").monotonic() < deadline and not server.done:
        server.handle_request()
    server.server_close()
    return 0 if server.done else 2


if __name__ == "__main__":
    raise SystemExit(main())
