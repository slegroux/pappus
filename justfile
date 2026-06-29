# SolveIt Sidekick — task runner (https://github.com/casey/just)
#
#   just            list recipes
#   just start         run the app in the background (pairs with `just stop`)
#   just start reload  same, but auto-restart the UI on source edits
#   just dev        start the kernel server + the web UI together (Ctrl+C stops both)
#   just kernel     start only the kernel server (the code-execution backend)
#   just ui         start only the web UI
#   just test       run the test suite
#   just doctor     check the kernel target is reachable

kernel_port := "5055"     # must match the `kernel` target's url in targets.yaml
ui_port     := "8000"

# Show the available recipes.
default:
    @just --list

# Pass `reload` to auto-restart the UI on source edits (`just start reload`);
# a reload resets the UI's in-memory notebook state — good for UI/CSS work.
# Start the app (kernel + UI) in the BACKGROUND; pairs with `just stop`.
start reload="":
    #!/usr/bin/env bash
    set -uo pipefail
    LOG="$HOME/Library/Logs/SolveItSidekick"; mkdir -p "$LOG"
    up(){ curl -s -o /dev/null "http://localhost:$1/" 2>/dev/null; }
    if up {{ui_port}}; then
        echo "✓ already running → http://localhost:{{ui_port}}"; open "http://localhost:{{ui_port}}" || true; exit 0
    fi
    RELOAD_ENV=""
    if [ "{{reload}}" = "reload" ]; then RELOAD_ENV="SIDEKICK_RELOAD=1"; echo "  (auto-reload on)"; fi
    if ! up {{kernel_port}}; then
        echo "▶ kernel server → :{{kernel_port}}  (logs: $LOG/kernel.log)"
        nohup uv run --extra kernel python -m server.kernel_server --port {{kernel_port}} >"$LOG/kernel.log" 2>&1 &
        for i in $(seq 1 120); do up {{kernel_port}} && break; sleep 0.5; done
    fi
    echo "▶ web UI → http://localhost:{{ui_port}}  (logs: $LOG/ui.log)"
    nohup env $RELOAD_ENV SIDEKICK_TARGET=kernel SIDEKICK_PORT={{ui_port}} uv run python -m sidekick.cli serve >"$LOG/ui.log" 2>&1 &
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
    uv run --extra kernel python -m server.kernel_server --port {{kernel_port}} &
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
    SIDEKICK_TARGET=kernel SIDEKICK_PORT={{ui_port}} uv run python -m sidekick.cli serve

# Just the kernel server (e.g. to run it on its own / on the H100).
kernel:
    uv run --extra kernel python -m server.kernel_server --port {{kernel_port}}

# Just the web UI (assumes the kernel server is already running).
ui:
    SIDEKICK_TARGET=kernel SIDEKICK_PORT={{ui_port}} uv run python -m sidekick.cli serve

# Run the test suite.
test:
    uv run pytest -q

# Diagnose the kernel target (DNS, port, token, /test_route).
doctor:
    uv run python -m sidekick.cli doctor kernel

# Stop the app — the kernel server and the UI (pairs with `just start`).
stop:
    #!/usr/bin/env bash
    for p in {{kernel_port}} {{ui_port}}; do
        pids=$(lsof -ti:$p 2>/dev/null || true)
        [ -n "$pids" ] && { echo "$pids" | xargs kill 2>/dev/null || true; echo "stopped :$p"; } \
                       || echo ":$p already free"
    done

# Build a double-click "SolveIt Sidekick.app" launcher (starts servers, opens browser).
app:
    #!/usr/bin/env bash
    set -euo pipefail
    APP="{{justfile_directory()}}/SolveIt Sidekick.app"
    rm -rf "$APP"
    mkdir -p "$APP/Contents/MacOS"
    cat > "$APP/Contents/Info.plist" <<'PLIST'
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
    <plist version="1.0"><dict>
      <key>CFBundleName</key><string>SolveIt Sidekick</string>
      <key>CFBundleDisplayName</key><string>SolveIt Sidekick</string>
      <key>CFBundleIdentifier</key><string>com.slegroux.solveit-sidekick</string>
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
    LOG="$HOME/Library/Logs/SolveItSidekick"; mkdir -p "$LOG"
    cd "$PROJ" || exit 1
    up(){ curl -s -o /dev/null "http://localhost:$1/" 2>/dev/null; }
    KPID=""; UPID=""
    # 1) kernel server on :5055 (start only if not already up)
    if ! up 5055; then
        nohup "$UV" run --extra kernel python -m server.kernel_server --port 5055 \
            >"$LOG/kernel.log" 2>&1 & KPID=$!
        for i in $(seq 1 120); do up 5055 && break; sleep 0.5; done
    fi
    # 2) UI on :8000 — after the kernel, so it connects live (not the mock)
    if ! up 8000; then
        nohup env SIDEKICK_TARGET=kernel SIDEKICK_PORT=8000 "$UV" run python -m sidekick.cli serve \
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
    echo "  Logs: ~/Library/Logs/SolveItSidekick/"
