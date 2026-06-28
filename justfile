# SolveIt Sidekick — task runner (https://github.com/casey/just)
#
#   just            list recipes
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
