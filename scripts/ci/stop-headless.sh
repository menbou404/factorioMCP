#!/usr/bin/env bash
set -uo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
FACTORIO_CI_DIR="${FACTORIO_CI_DIR:-$repo_root/.factorio-ci}"
pid_file="$FACTORIO_CI_DIR/server.pid"

if [[ -f "$pid_file" ]]; then
    server_pid=""
    read -r server_pid < "$pid_file" || true
    if [[ "$server_pid" =~ ^[1-9][0-9]*$ ]] && kill -0 "$server_pid" 2>/dev/null; then
        kill -TERM "$server_pid" 2>/dev/null || true
        started_at=$SECONDS
        while kill -0 "$server_pid" 2>/dev/null && (( SECONDS - started_at < 10 )); do
            sleep 1
        done
        if kill -0 "$server_pid" 2>/dev/null; then
            kill -KILL "$server_pid" 2>/dev/null || true
        fi
    fi
    rm -f -- "$pid_file" || true
fi

exit 0
