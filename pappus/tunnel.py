"""SSH tunnel management for remote SolveIt targets (e.g. the H100).

We forward the remote SolveIt port to a local port so the client always talks
to localhost. This keeps the H100's SolveIt server off the public internet and
makes "switching targets" a no-op for the client code.
"""
from __future__ import annotations

import socket
import subprocess
import time
from contextlib import closing

from .targets import Target


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def _start_remote_server(t: Target) -> None:
    """Optionally (re)start the SolveIt server on the remote box before tunneling."""
    if not (t.ssh and t.ssh.start_cmd):
        return
    subprocess.run(
        ["ssh", t.ssh.host, t.ssh.start_cmd],
        check=True,
        timeout=60,
    )


def open_tunnel(t: Target, wait: float = 8.0) -> subprocess.Popen:
    """Open an SSH local-forward tunnel for a remote target and wait until it's live.

    Returns the Popen handle. Caller is responsible for .terminate().
    """
    if not t.is_remote:
        raise ValueError(f"Target '{t.name}' is local; no tunnel needed.")
    ssh = t.ssh

    if port_open("127.0.0.1", ssh.local_port):
        raise RuntimeError(
            f"Local port {ssh.local_port} is already in use. "
            f"A tunnel may already be running, or another process owns it."
        )

    _start_remote_server(t)

    # -N: no remote command, -T: no tty, ExitOnForwardFailure so we fail fast.
    cmd = [
        "ssh",
        "-N",
        "-T",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-L", f"{ssh.local_port}:127.0.0.1:{ssh.remote_port}",
        ssh.host,
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    deadline = time.time() + wait
    while time.time() < deadline:
        if proc.poll() is not None:
            err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            raise RuntimeError(f"SSH tunnel exited early:\n{err.strip()}")
        if port_open("127.0.0.1", ssh.local_port):
            return proc
        time.sleep(0.3)

    proc.terminate()
    raise TimeoutError(
        f"Tunnel to {ssh.host} did not come up within {wait}s "
        f"(remote {ssh.remote_port} -> local {ssh.local_port})."
    )


def close_tunnel(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
