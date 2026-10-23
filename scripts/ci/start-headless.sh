#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
FACTORIO_VERSION="${FACTORIO_VERSION:-2.0.77}"
FACTORIO_ARCHIVE_DIR="${FACTORIO_ARCHIVE_DIR:-$HOME/.cache/factorio-headless}"
if [[ -n "${RUNNER_TEMP:-}" ]]; then
    default_root="$RUNNER_TEMP/factorio"
else
    default_root="$HOME/factorio-root"
fi
FACTORIO_ROOT="${FACTORIO_ROOT:-$default_root}"
FACTORIO_CI_DIR="${FACTORIO_CI_DIR:-$repo_root/.factorio-ci}"
RCON_PORT="${RCON_PORT:-27277}"

archive="$FACTORIO_ARCHIVE_DIR/factorio-headless_linux_${FACTORIO_VERSION}.tar.xz"
mkdir -p "$FACTORIO_ARCHIVE_DIR" "$FACTORIO_ROOT"
if [[ ! -f "$archive" ]]; then
    archive_tmp="$(mktemp "${archive}.tmp.XXXXXX")"
    trap 'rm -f -- "$archive_tmp"' EXIT
    curl -fsSL -o "$archive_tmp" \
        "https://factorio.com/get-download/${FACTORIO_VERSION}/headless/linux64"
    mv -- "$archive_tmp" "$archive"
    trap - EXIT
fi
tar -xJf "$archive" -C "$FACTORIO_ROOT"
factorio="$FACTORIO_ROOT/factorio/bin/x64/factorio"
"$factorio" --version

mkdir -p "$FACTORIO_CI_DIR/mods"
mod_packages=("$repo_root"/mod/factorio_ai_mcp_*)
if [[ ${#mod_packages[@]} -ne 1 || ! -d "${mod_packages[0]}" ]]; then
    echo "Expected a single mod/factorio_ai_mcp_* package." >&2
    exit 1
fi
cp -r -- "${mod_packages[0]}" "$FACTORIO_CI_DIR/mods/"
python3 - "$FACTORIO_ROOT/factorio/data" "$FACTORIO_CI_DIR/mods/mod-list.json" <<'PY'
import json
import sys
from pathlib import Path

data = Path(sys.argv[1])
mods = [{"name": "base", "enabled": True}]
for name in ("elevated-rails", "quality", "space-age"):
    if (data / name).is_dir():
        mods.append({"name": name, "enabled": True})
mods.append({"name": "factorio_ai_mcp", "enabled": True})
with Path(sys.argv[2]).open("w", encoding="utf-8") as output:
    json.dump({"mods": mods}, output, indent=2)
    output.write("\n")
print("Enabled mods:", [mod["name"] for mod in mods])
PY

cat > "$FACTORIO_CI_DIR/server-settings.json" <<'JSON'
{
  "name": "factorio-mcp-ci",
  "description": "ci",
  "max_players": 0,
  "visibility": {"public": false, "lan": false},
  "require_user_verification": false,
  "auto_pause": false,
  "autosave_interval": 0,
  "allow_commands": "admins-only"
}
JSON

mkdir -p "$repo_root/.factorio-test"
secret_file="$repo_root/.factorio-test/rcon-secret.txt"
(umask 077; openssl rand -hex 16 > "$secret_file")

"$factorio" --mod-directory "$FACTORIO_CI_DIR/mods" \
    --create "$FACTORIO_CI_DIR/ci-save.zip" 2>&1 | tail -20

server_log="$FACTORIO_CI_DIR/factorio-server.log"
nohup "$factorio" --mod-directory "$FACTORIO_CI_DIR/mods" \
    --start-server "$FACTORIO_CI_DIR/ci-save.zip" \
    --server-settings "$FACTORIO_CI_DIR/server-settings.json" \
    --rcon-bind "127.0.0.1:$RCON_PORT" --rcon-password "$(cat "$secret_file")" \
    < /dev/null > "$server_log" 2>&1 &
server_pid=$!
printf '%s\n' "$server_pid" > "$FACTORIO_CI_DIR/server.pid"

started_at=$SECONDS
while (( SECONDS - started_at < 90 )); do
    if ! kill -0 "$server_pid" 2>/dev/null; then
        echo "Factorio server exited before RCON was ready." >&2
        tail -60 "$server_log"
        exit 1
    fi
    if grep -q "Starting RCON interface" "$server_log"; then
        echo "RCON ready after $((SECONDS - started_at))s"
        exit 0
    fi
    sleep 1
done

echo "Timed out after 90s waiting for RCON." >&2
tail -60 "$server_log"
exit 1
