#!/usr/bin/env python3
import argparse
import ctypes
import html
import http.server
import os
import secrets
import signal
import urllib.parse
from typing import cast


class Server(http.server.HTTPServer):
    organization: str
    state: str
    manifest: str
    output: str
    handoff: str
    done: bool


class Callback(http.server.BaseHTTPRequestHandler):
    server_version = "ci-fleet-bootstrap"
    sys_version = ""

    @property
    def app_server(self) -> Server:
        return cast(Server, self.server)

    def log_message(self, _format: str, *_args: object) -> None:
        return

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
        code = values["code"][0]
        if not code or len(code) > 512 or any(character.isspace() for character in code):
            self.reply(400, "Invalid callback")
            return
        try:
            descriptor = os.open(self.app_server.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            self.reply(409, "Callback already consumed")
            return
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(code)
        self.reply(200, "<meta http-equiv=refresh content='1;url=/next'><h1>Registration received</h1><p>Preparing installation approval. No value needs to be copied.</p>")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--organization", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--handoff", required=True)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 30 <= args.timeout <= 1800:
        parser.error("invalid port or timeout")
    parent = os.getppid()
    if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM) != 0:
        raise OSError(ctypes.get_errno(), "cannot bind callback lifetime to bootstrap")
    if os.getppid() != parent:
        return 2
    server = Server((args.bind, args.port), Callback)
    server.organization = args.organization
    server.state = args.state
    server.manifest = args.manifest
    server.output = args.output
    server.handoff = args.handoff
    server.done = False
    server.timeout = 1
    deadline = __import__("time").monotonic() + args.timeout
    while __import__("time").monotonic() < deadline and not server.done:
        server.handle_request()
    server.server_close()
    return 0 if server.done else 2


if __name__ == "__main__":
    raise SystemExit(main())
