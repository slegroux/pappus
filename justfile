# Pappus — task runner (https://github.com/casey/just)
#
#   just            list recipes
#   just start         run the app in the background (pairs with `just stop`)
#   just start reload  same, but auto-restart the UI on source edits
#   just dev        start the kernel server + the web UI together (Ctrl+C stops both)
#   just kernel     start only the kernel server (the code-execution backend)
#   just ui         start only the web UI
#   just test       run the test suite
#   just doctor     check the kernel target is reachable
#   just backup     pack all local data (notebooks, papers, …) into one .tar.gz
#   just restore F  unpack a backup into this install (app stopped)

kernel_port := "5055"     # must match the `kernel` target's url in targets.yaml
ui_port     := "8000"

# Extras every app process runs with. `paper` (marker) is included by default so
# opening a PDF gets real structure — headings/sections/equations — out of the box
# (pypdf, the no-marker fallback, produces flat text with no sections). The kernel
# and UI share one uv-managed venv, so BOTH must request the same set or `uv run`
# re-syncs (uninstalls the other's extras) between launches.
run_extras := "--extra kernel --extra paper"

# Notebooks, papers, recall, libraries and blog live LOCALLY in
# ~/.config/pappus (the app's default; override with PAPPUS_DATA),
# never in this repo. Move them between installs with `just backup`/`just restore`.

# This machine's own Tailscale MagicDNS name (empty when off-tailnet or Tailscale
# isn't installed). Passed to the UI as PAPPUS_ALLOWED_HOSTS so `tailscale serve`
# can front the UI for other tailnet devices — the app still only accepts LOOPBACK
# peers (see _LocalGuard in app.py), so this just lets the Host-header check pass.
# Derived per-machine, never hardcoded (the justfile is shared across machines).
tailnet_host := `tailscale status --json 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin).get('Self',{}).get('DNSName','').rstrip('.'))" 2>/dev/null || true`

# On a tailnet machine, trust the tailnet so `tailscale serve` works: on macOS it
# forwards each request to the loopback-bound UI carrying the ORIGINATING tailnet IP
# as the peer, which the UI's primary peer gate would otherwise reject. Off-tailnet
# this is "0" (loopback-only, unchanged). The UI still binds to 127.0.0.1, so the
# only way a tailnet peer reaches it is through `tailscale serve`.
trust_tailnet := if tailnet_host != "" { "1" } else { "0" }

# Codex-via-Portkey: when PORTKEY_API_KEY is set, the kernel's "Codex" model is
# routed through the Portkey gateway (Azure OpenAI behind it) instead of hitting
# api.openai.com with the global OPENAI_API_KEY. Scoped to the kernel process
# only, so other tools keep seeing the unmodified environment. The model must be
# an Azure *deployment* name; override with OPENAI_MODEL if yours differs.
# Recipes paste this snippet and pass $PKENV via `env` at kernel launch.
portkey_env := '''
    PKENV=""
    if [ -n "${PORTKEY_API_KEY:-}" ]; then
        PKENV="OPENAI_BASE_URL=https://api.portkey.ai/v1 OPENAI_API_KEY=$PORTKEY_API_KEY OPENAI_MODEL=${OPENAI_MODEL:-gpt-5.5}"
    fi
'''

# Show the available recipes.
default:
    @just --list

# Pass `reload` to auto-restart the UI on source edits (`just start reload`);
# a reload resets the UI's in-memory notebook state — good for UI/CSS work.
# Start the app (kernel + UI) in the BACKGROUND; pairs with `just stop`.
start reload="":
    #!/usr/bin/env bash
    set -uo pipefail
    LOG="$HOME/Library/Logs/Pappus"; mkdir -p "$LOG"
    up(){ curl -s -o /dev/null "http://localhost:$1/" 2>/dev/null; }
    if up {{ui_port}}; then
        echo "✓ already running → http://localhost:{{ui_port}}"; open "http://localhost:{{ui_port}}" || true; exit 0
    fi
    RELOAD_ENV=""
    if [ "{{reload}}" = "reload" ]; then RELOAD_ENV="PAPPUS_RELOAD=1"; echo "  (auto-reload on)"; fi
    if ! up {{kernel_port}}; then
        echo "▶ kernel server → :{{kernel_port}}  (logs: $LOG/kernel.log)"
        {{portkey_env}}
        nohup env $PKENV uv run {{run_extras}} python -m server.kernel_server --port {{kernel_port}} >"$LOG/kernel.log" 2>&1 &
        for i in $(seq 1 120); do up {{kernel_port}} && break; sleep 0.5; done
    fi
    echo "▶ web UI → http://localhost:{{ui_port}}  (logs: $LOG/ui.log)"
    nohup env $RELOAD_ENV PAPPUS_ALLOWED_HOSTS="{{tailnet_host}}" PAPPUS_TRUST_TAILNET="{{trust_tailnet}}" PAPPUS_TARGET=kernel PAPPUS_PORT={{ui_port}} uv run {{run_extras}} python -m pappus.cli serve >"$LOG/ui.log" 2>&1 &
    for i in $(seq 1 60); do up {{ui_port}} && break; sleep 0.5; done
    if up {{ui_port}}; then
        echo "✓ running in the background → http://localhost:{{ui_port}}   (stop with: just stop)"
        open "http://localhost:{{ui_port}}" || true
    else
        echo "✗ UI didn't come up — check $LOG/ui.log"; exit 1
    fi

# Start the kernel server (backend) + the web UI together; Ctrl+C stops both.
dev:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "▶ kernel server  → http://localhost:{{kernel_port}}  (--extra kernel: numpy/torch/…)"
    {{portkey_env}}
    env $PKENV uv run {{run_extras}} python -m server.kernel_server --port {{kernel_port}} &
    KERNEL_PID=$!
    trap 'echo; echo "■ stopping…"; kill $KERNEL_PID 2>/dev/null || true; \
          lsof -ti:{{kernel_port}} | xargs kill 2>/dev/null || true' INT TERM EXIT
    printf "  waiting for the kernel"
    for i in $(seq 1 60); do
        curl -s -o /dev/null "http://localhost:{{kernel_port}}/" && break
        printf "."; sleep 0.5
    done
    echo " ✓"
    echo "▶ web UI         → http://localhost:{{ui_port}}  (target: kernel)"
    PAPPUS_ALLOWED_HOSTS="{{tailnet_host}}" PAPPUS_TRUST_TAILNET="{{trust_tailnet}}" PAPPUS_TARGET=kernel PAPPUS_PORT={{ui_port}} uv run {{run_extras}} python -m pappus.cli serve

# Just the kernel server (e.g. to run it on its own / on the H100).
kernel:
    #!/usr/bin/env bash
    set -euo pipefail
    {{portkey_env}}
    exec env $PKENV uv run {{run_extras}} python -m server.kernel_server --port {{kernel_port}}

# Just the web UI (assumes the kernel server is already running).
ui:
    PAPPUS_ALLOWED_HOSTS="{{tailnet_host}}" PAPPUS_TRUST_TAILNET="{{trust_tailnet}}" PAPPUS_TARGET=kernel PAPPUS_PORT={{ui_port}} uv run {{run_extras}} python -m pappus.cli serve

# `just backup ~/pCloud/` writes a timestamped archive there; API keys are left
# out unless you pass --with-secrets.
# Pack all local data (notebooks, papers, recall, libraries, blog) into one .tar.gz
backup *args:
    uv run python -m pappus.cli backup {{args}}

# Stop the app first (`just stop`); existing files are kept unless --force.
# Unpack a backup into this install
restore archive *args:
    uv run python -m pappus.cli restore {{archive}} {{args}}

# Run the test suite.
test:
    uv run pytest -q

# Lint with ruff (add `--fix` by hand for autofixes: `uv run ruff check --fix`).
lint:
    uv run ruff check

# Diagnose the kernel target (DNS, port, token, /test_route).
doctor:
    uv run python -m pappus.cli doctor kernel

# Stop the app — the kernel server and the UI (pairs with `just start`).
stop:
    #!/usr/bin/env bash
    for p in {{kernel_port}} {{ui_port}}; do
        pids=$(lsof -ti:$p 2>/dev/null || true)
        [ -n "$pids" ] && { echo "$pids" | xargs kill 2>/dev/null || true; echo "stopped :$p"; } \
                       || echo ":$p already free"
    done

# Build a double-click "Pappus.app" launcher (starts servers, opens browser).
app:
    #!/usr/bin/env bash
    set -euo pipefail
    APP="{{justfile_directory()}}/Pappus.app"
    rm -rf "$APP"
    mkdir -p "$APP/Contents/MacOS"
    cat > "$APP/Contents/Info.plist" <<'PLIST'
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
    <plist version="1.0"><dict>
      <key>CFBundleName</key><string>Pappus</string>
      <key>CFBundleDisplayName</key><string>Pappus</string>
      <key>CFBundleIdentifier</key><string>com.slegroux.pappus</string>
      <key>CFBundleVersion</key><string>1.0</string>
      <key>CFBundleShortVersionString</key><string>1.0</string>
      <key>CFBundlePackageType</key><string>APPL</string>
      <key>CFBundleExecutable</key><string>launcher</string>
    </dict></plist>
    PLIST
    cat > "$APP/Contents/MacOS/launcher" <<'LAUNCH'
    #!/bin/bash
    # GUI apps inherit a minimal PATH — restore the tools we need.
    export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    PROJ="__PROJ__"
    UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
    LOG="$HOME/Library/Logs/Pappus"; mkdir -p "$LOG"
    cd "$PROJ" || exit 1
    up(){ curl -s -o /dev/null "http://localhost:$1/" 2>/dev/null; }
    KPID=""; UPID=""
    # 1) kernel server on :5055 (start only if not already up).
    # Codex-via-Portkey (see the justfile's portkey_env): GUI apps don't source
    # .zshrc, so pull PORTKEY_API_KEY from ~/.secrets.env before checking it.
    [ -f "$HOME/.secrets.env" ] && source "$HOME/.secrets.env"
    PKENV=""
    if [ -n "${PORTKEY_API_KEY:-}" ]; then
        PKENV="OPENAI_BASE_URL=https://api.portkey.ai/v1 OPENAI_API_KEY=$PORTKEY_API_KEY OPENAI_MODEL=${OPENAI_MODEL:-gpt-5.5}"
    fi
    if ! up 5055; then
        nohup env $PKENV "$UV" run {{run_extras}} python -m server.kernel_server --port 5055 \
            >"$LOG/kernel.log" 2>&1 & KPID=$!
        for i in $(seq 1 120); do up 5055 && break; sleep 0.5; done
    fi
    # 2) UI on :8000 — after the kernel, so it connects live (not the mock)
    if ! up 8000; then
        nohup env PAPPUS_TARGET=kernel PAPPUS_PORT=8000 "$UV" run {{run_extras}} python -m pappus.cli serve \
            >"$LOG/ui.log" 2>&1 & UPID=$!
        for i in $(seq 1 60); do up 8000 && break; sleep 0.5; done
    fi
    # The servers keep running in the background (nohup); the launcher opens the
    # browser and exits. Stop everything with `just stop`. (We don't tie shutdown
    # to "Quit" — a script-based .app doesn't receive Quit reliably.)
    open "http://localhost:8000"
    LAUNCH
    sed -i '' "s|__PROJ__|{{justfile_directory()}}|" "$APP/Contents/MacOS/launcher"
    chmod +x "$APP/Contents/MacOS/launcher"
    touch "$APP"                                  # nudge LaunchServices to register it
    echo "✓ Built: $APP"
    echo "  Double-click it, or drag it to /Applications and your Dock."
    echo "  Logs: ~/Library/Logs/Pappus/"
