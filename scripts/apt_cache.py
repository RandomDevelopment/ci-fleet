#!/usr/bin/env python3
"""Render optional host-local APT cache configuration without printing endpoints."""

from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit


def valid_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return len(host) <= 253 and all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in host.removesuffix(".").split(".")
        )


def apt_config(proxy: str, no_proxy: str = "") -> str:
    if not proxy:
        if no_proxy:
            raise ValueError("CI_FLEET_NO_PROXY requires CI_FLEET_APT_HTTP_PROXY")
        return ""
    error = "CI_FLEET_APT_HTTP_PROXY must be a credential-free HTTP origin URL"
    if len(proxy) > 2048 or not re.fullmatch(r"http://[A-Za-z0-9.:[\]-]+/?", proxy):
        raise ValueError(error)
    try:
        parsed = urlsplit(proxy)
        if (not parsed.hostname or not valid_host(parsed.hostname)
                or parsed.path not in ("", "/") or parsed.username is not None
                or parsed.query or parsed.fragment
                or (parsed.netloc.startswith("[") and ":" not in parsed.hostname)
                or parsed.netloc.endswith(":")
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            raise ValueError(error)
    except ValueError:
        raise ValueError(error) from None
    hosts = no_proxy.split(",") if no_proxy else []
    if len(hosts) > 64 or any(not valid_host(host) or ":" in host for host in hosts):
        raise ValueError("CI_FLEET_NO_PROXY must contain at most 64 comma-separated DNS names or IPv4 addresses")
    lines = [f'Acquire::http::Proxy "{proxy}";', 'Acquire::https::Proxy "DIRECT";',
             'Acquire::http::Timeout "10";', 'Acquire::Retries "0";',
             'APT::Update::Error-Mode "any";']
    lines += [f'Acquire::http::Proxy::{host} "DIRECT";' for host in hosts]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        content = apt_config(os.environ.get("CI_FLEET_APT_HTTP_PROXY", ""),
                             os.environ.get("CI_FLEET_NO_PROXY", ""))
        # The caller owns this private temporary directory and removes it on every exit.
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
    except (ValueError, OSError) as exc:
        print(str(exc) if isinstance(exc, ValueError) else "APT cache configuration could not be written", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
