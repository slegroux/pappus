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
#   just sync       commit/pull/push the dialog store (data/) across machines

kernel_port := "5055"     # must match the `kernel` target's url in targets.yaml
ui_port     := "8000"

# Dialogs/papers/blog live IN the repo (data/) — this repo is private and doubles
# as the cross-machine sync + backup channel for them (`just sync`). Secrets stay
# outside (~/.config/solveit-sidekick/secrets.json, see .gitignore).
data_dir := justfile_directory() / "data"

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
    LOG="$HOME/Library/Logs/SolveItSidekick"; mkdir -p "$LOG"
    up(){ curl -s -o /dev/null "http://localhost:$1/" 2>/dev/null; }
    if up {{ui_port}}; then
        echo "✓ already running → http://localhost:{{ui_port}}"; open "http://localhost:{{ui_port}}" || true; exit 0
    fi
    RELOAD_ENV=""
    if [ "{{reload}}" = "reload" ]; then RELOAD_ENV="SIDEKICK_RELOAD=1"; echo "  (auto-reload on)"; fi
    if ! up {{kernel_port}}; then
        echo "▶ kernel server → :{{kernel_port}}  (logs: $LOG/kernel.log)"
        {{portkey_env}}
        nohup env $PKENV uv run --extra kernel python -m server.kernel_server --port {{kernel_port}} >"$LOG/kernel.log" 2>&1 &
        for i in $(seq 1 120); do up {{kernel_port}} && break; sleep 0.5; done
    fi
    echo "▶ web UI → http://localhost:{{ui_port}}  (logs: $LOG/ui.log)"
    nohup env $RELOAD_ENV SIDEKICK_TARGET=kernel SIDEKICK_PORT={{ui_port}} SIDEKICK_DATA={{data_dir}} uv run python -m sidekick.cli serve >"$LOG/ui.log" 2>&1 &
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
    env $PKENV uv run --extra kernel python -m server.kernel_server --port {{kernel_port}} &
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
    SIDEKICK_TARGET=kernel SIDEKICK_PORT={{ui_port}} SIDEKICK_DATA={{data_dir}} uv run python -m sidekick.cli serve

# Just the kernel server (e.g. to run it on its own / on the H100).
kernel:
    #!/usr/bin/env bash
    set -euo pipefail
    {{portkey_env}}
    exec env $PKENV uv run --extra kernel python -m server.kernel_server --port {{kernel_port}}

# Just the web UI (assumes the kernel server is already running).
ui:
    SIDEKICK_TARGET=kernel SIDEKICK_PORT={{ui_port}} SIDEKICK_DATA={{data_dir}} uv run python -m sidekick.cli serve

# Sync dialogs across machines: commit local dialog/paper changes, pull, push.
# Run at session start (get the other machine's dialogs) and session end (share
# this one's). Conflicts are rare (one machine at a time) but resolve manually.
sync:
    #!/usr/bin/env bash
    set -euo pipefail
    cd {{justfile_directory()}}
    git add data
    git diff --cached --quiet -- data || git commit -m "data: sync dialogs ($(hostname -s))"
    git pull --rebase --autostash
    git push
    echo "✓ dialogs in sync"

# Run the test suite.
test:
    uv run pytest -q

# Lint with ruff (add `--fix` by hand for autofixes: `uv run ruff check --fix`).
lint:
    uv run ruff check

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
    # 1) kernel server on :5055 (start only if not already up).
    # Codex-via-Portkey (see the justfile's portkey_env): GUI apps don't source
    # .zshrc, so pull PORTKEY_API_KEY from ~/.secrets.env before checking it.
    [ -f "$HOME/.secrets.env" ] && source "$HOME/.secrets.env"
    PKENV=""
    if [ -n "${PORTKEY_API_KEY:-}" ]; then
        PKENV="OPENAI_BASE_URL=https://api.portkey.ai/v1 OPENAI_API_KEY=$PORTKEY_API_KEY OPENAI_MODEL=${OPENAI_MODEL:-gpt-5.5}"
    fi
    if ! up 5055; then
        nohup env $PKENV "$UV" run --extra kernel python -m server.kernel_server --port 5055 \
            >"$LOG/kernel.log" 2>&1 & KPID=$!
        for i in $(seq 1 120); do up 5055 && break; sleep 0.5; done
    fi
    # 2) UI on :8000 — after the kernel, so it connects live (not the mock)
    if ! up 8000; then
        nohup env SIDEKICK_TARGET=kernel SIDEKICK_PORT=8000 SIDEKICK_DATA="$PROJ/data" "$UV" run python -m sidekick.cli serve \
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
