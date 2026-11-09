#!/usr/bin/env bash
# Refreshes graphify-out/ when an agent session or turn ends. Called by the
# Cursor (.cursor/hooks.json), Claude Code (.claude/settings.json) and Codex
# (.codex/hooks.json) hooks. It must return at once: Codex caps SessionEnd
# hooks at 3 seconds, so the rebuild runs detached and serialised by a lock.
set -u

[ -t 0 ] || cat >/dev/null

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
graphify=$(command -v graphify || echo "$HOME/.local/bin/graphify")

if [ -x "$graphify" ] && [ -f "$root/graphify-out/graph.json" ]; then
    GRAPHIFY_BIN=$graphify PYTHONHASHSEED=0 setsid bash -c '
        cd "$1" || exit 0
        exec 9>graphify-out/.update.lock
        flock -w 600 9 || exit 0
        "$GRAPHIFY_BIN" update . >graphify-out/.update.log 2>&1
    ' _ "$root" </dev/null >/dev/null 2>&1 &
fi

echo '{}'
