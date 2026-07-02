"""sidekick CLI — manage tunnels, diagnose connections, launch the UI.

Usage:
    sidekick targets                 list configured targets
    sidekick doctor [name]           diagnose a target's connection
    sidekick up <name>               open the SSH tunnel for a remote target (blocks)
    sidekick serve                   launch the web UI (http://localhost:8000)
    sidekick library build <lib> <dest> [--pkg <name>]
                                     project cells tagged `#| export <lib>:<mod>`
                                     across all dialogs into an nbdev project at
                                     <dest>, then build it with nbdev if installed
"""
from __future__ import annotations

import os
import sys

from .targets import get_target, list_targets, load_config


def cmd_targets(_):
    cfg = load_config()
    default = cfg.get("default")
    for name in list_targets():
        t = get_target(name)
        kind = f"remote via {t.ssh.host}" if t.is_remote else "local"
        star = " (default)" if name == default else ""
        print(f"  {name:8} {t.url:28} [{kind}]{star}")
    return 0


def cmd_doctor(args):
    from .doctor import run_checks
    name = args[0] if args else None
    t = get_target(name)
    print(f"Diagnosing target '{t.name}' -> {t.url}\n")
    ok_all = True
    for ok, label, detail in run_checks(t):
        print(f"  [{'OK ' if ok else 'XX '}] {label:7} {detail}")
        ok_all = ok_all and ok
    print("\n" + ("All checks passed." if ok_all else "Some checks failed — see above."))
    return 0 if ok_all else 1


def cmd_up(args):
    from .tunnel import open_tunnel, close_tunnel
    if not args:
        print("usage: sidekick up <target>", file=sys.stderr)
        return 2
    t = get_target(args[0])
    if not t.is_remote:
        print(f"Target '{t.name}' is local — no tunnel needed.")
        return 0
    print(f"Opening tunnel {t.ssh.host}:{t.ssh.remote_port} -> localhost:{t.ssh.local_port} …")
    proc = open_tunnel(t)
    print(f"Tunnel up. Point the UI at target '{t.name}'. Ctrl-C to close.")
    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\nClosing tunnel…")
        close_tunnel(proc)
    return 0


def cmd_serve(_):
    import uvicorn
    # Default to loopback: the UI is single-user with global state and holds your
    # API keys. Opt into network exposure explicitly via SIDEKICK_HOST=0.0.0.0.
    host = os.environ.get("SIDEKICK_HOST", "127.0.0.1")
    port = int(os.environ.get("SIDEKICK_PORT", "8000"))
    # SIDEKICK_RELOAD=1 → auto-restart on source edits (dev convenience). Reload
    # needs an import string rather than the app object so uvicorn can re-import
    # the module in the worker; without it we pass the object directly.
    reload = os.environ.get("SIDEKICK_RELOAD", "").lower() in ("1", "true", "yes")
    print(f"SolveIt Sidekick UI -> http://{host}:{port}" + ("  (auto-reload)" if reload else ""))
    if host not in ("127.0.0.1", "localhost"):
        print("  ⚠ binding a non-loopback host — this single-user UI (and your stored "
              "API keys) will be reachable by anyone on the network.")
    if reload:
        uvicorn.run("sidekick.app:app", host=host, port=port,
                    reload=True, reload_dirs=[os.path.dirname(__file__)])
    else:
        from .app import app
        uvicorn.run(app, host=host, port=port)
    return 0


def cmd_library(args):
    """`sidekick library build <lib> <dest> [--pkg <name>]` — emit + build a library."""
    from .client import connect
    from . import nbdev_export
    if len(args) < 3 or args[0] != "build":
        print("usage: sidekick library build <lib> <dest> [--pkg <name>]", file=sys.stderr)
        return 2
    lib, dest = args[1], args[2]
    pkg = None
    if "--pkg" in args:
        i = args.index("--pkg")
        pkg = args[i + 1] if i + 1 < len(args) else None
    backend, warning = connect(get_target())
    if warning:
        print(f"  ⚠ {warning}")
    result = nbdev_export.build_library(backend, lib, dest, pkg)
    if not result["modules"]:
        print(f"No cells tagged `#| export {lib}:<module>` found across dialogs — "
              f"emitted an empty scaffold at {dest}.")
    else:
        print(f"Built library '{result['pkg']}' at {dest}")
        print(f"  modules: {', '.join(result['modules'])}")
        print(f"  files:   {len(result['files'])}")
    print(f"  nbdev:   {'✓ ' if result['nbdev_ok'] else '· '}{result['nbdev_detail']}")
    return 0


COMMANDS = {"targets": cmd_targets, "doctor": cmd_doctor, "up": cmd_up,
            "serve": cmd_serve, "library": cmd_library}


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] not in COMMANDS:
        print(__doc__)
        return 0 if not argv else 2
    return COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
