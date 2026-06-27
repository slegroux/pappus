"""Connection diagnostics — kills the 'why won't it connect' setup pain.

Each check returns (ok, label, detail). The CLI prints them as a checklist so
you can see exactly where local-vs-H100 setup breaks down.
"""
from __future__ import annotations

import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse

from .targets import Target
from .tunnel import port_open

Check = tuple[bool, str, str]


def _host_port(url: str) -> tuple[str, int]:
    p = urlparse(url)
    return p.hostname or "localhost", p.port or (443 if p.scheme == "https" else 80)


def check_dns(host: str) -> Check:
    if host in ("localhost", "127.0.0.1"):
        return True, "DNS", f"{host} (loopback)"
    try:
        ip = socket.gethostbyname(host)
        return True, "DNS", f"{host} -> {ip}"
    except socket.gaierror as e:
        return False, "DNS", f"cannot resolve {host}: {e}"


def check_port(host: str, port: int) -> Check:
    ok = port_open(host, port, timeout=2.0)
    detail = f"{host}:{port} {'reachable' if ok else 'refused/closed'}"
    return ok, "PORT", detail


def check_test_route(t: Target) -> Check:
    """Hit SolveIt's /test_route with the token cookie, mirroring solveit_client."""
    url = t.url.rstrip("/") + "/test_route"
    req = urllib.request.Request(url)
    req.add_header("Cookie", f"_solveit={t.token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode(errors="replace").strip()
        if "here" in body.lower():
            return True, "AUTH", "/test_route OK (token accepted)"
        return False, "AUTH", f"/test_route returned unexpected body: {body[:80]!r}"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, "AUTH", f"{e.code}: token rejected — refresh your _solveit cookie"
        return False, "AUTH", f"HTTP {e.code} from /test_route"
    except urllib.error.URLError as e:
        return False, "AUTH", f"no response: {e.reason}"


def run_checks(t: Target) -> list[Check]:
    host, port = _host_port(t.url)
    checks: list[Check] = []

    if t.is_remote:
        # For remote targets the client URL is the *local* tunnel end.
        checks.append(check_dns(t.ssh.host))
        tunnel_up = port_open("127.0.0.1", t.ssh.local_port)
        checks.append((
            tunnel_up, "TUNNEL",
            f"local {t.ssh.local_port} {'up' if tunnel_up else 'down — run: sidekick up ' + t.name}",
        ))

    checks.append(check_dns(host))
    checks.append(check_port(host, port))
    if port_open(host, port, timeout=2.0):
        checks.append(check_test_route(t))
    return checks
