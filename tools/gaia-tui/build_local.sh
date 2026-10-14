#!/bin/bash
# Build a release-shaped GAIA from the integration worktree:
#   1. reset $GAIA_INT_WORKTREE to origin/main and merge the given fix branches
#   2. freeze gaia-agent.exe exactly as build_flagship_sidecar.yml does
#   3. build gaia-tui.exe
#   4. install the same core into venv-local for the daemon
# usage: build_local.sh [--no-reset] [branch ...]
#   --no-reset  build on the worktree's current HEAD instead of resetting it to origin/main
set -euo pipefail
INT="${GAIA_INT_WORKTREE:?set GAIA_INT_WORKTREE to an amd/gaia worktree used for integration builds}"
EVAL_HOME="${GAIA_TUI_EVAL_HOME:-$HOME/gaia-tui-eval}"
OUT="$EVAL_HOME/bin-local"
cd "$INT"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "ERROR: $INT has uncommitted changes; commit or move them before an integration build" >&2; exit 2
fi
git fetch -q origin
if [ "${1:-}" = "--no-reset" ]; then shift; else git reset -q --hard origin/main; fi
for b in "$@"; do
  if ! git merge -q --no-edit "origin/$b"; then
    if [ -n "${RESOLVE_SCRIPT:-}" ] && python "$RESOLVE_SCRIPT" && git commit -q --no-edit; then echo "auto-resolved $b"; else echo "MERGE CONFLICT on $b"; git merge --abort; exit 3; fi
  fi
  echo "merged $b"
done
SHA=$(git rev-parse --short HEAD)
.venv-freeze/Scripts/python.exe hub/agents/gaia/python/packaging/freeze.py --onefile > /tmp/freeze.log 2>&1 || { tail -30 /tmp/freeze.log; exit 4; }
mkdir -p "$OUT"
AGENT=hub/agents/gaia/python/packaging/dist/gaia-agent.exe
cp "$AGENT" "$OUT/gaia-agent.exe"
(cd tui && go build -trimpath -ldflags="-s -w -X github.com/amd/gaia/tui/internal/cli.version=local-$SHA -X github.com/amd/gaia/tui/internal/cli.commit=$SHA" -o "$OUT/gaia-tui.exe" ./cmd/gaia)
V="$EVAL_HOME/venv-local"
[ -d "$V" ] || uv venv -q "$V" --python 3.12
uv pip install -q --native-tls --python "$V/Scripts/python.exe" -e ".[api,rag]" 2>&1 | tail -2
echo "$SHA $*" > "$OUT/BUILD.txt"
echo "built $SHA -> $OUT ($(du -h "$OUT/gaia-agent.exe" | cut -f1) agent)"
