#!/bin/bash
# usage: rec_thread.sh <leg> <batch> <task> "<app time>" <model> [credits]  — record from a copied thread (clipboard)
# Cloud runs: pass the credits the app showed. The harness is perplexity-cloud for the cloud leg,
# perplexity-lemonade for the lemonade leg, perplexity-local otherwise.
set -euo pipefail
. "$(dirname "$0")/_env.sh"
[ $# -ge 5 ] || { echo 'usage: rec_thread.sh <leg> <batch> <task> "<app time>" <model> [credits]' >&2; exit 2; }
L=$1; B=$2; T=$3; APP=$4; M=$5; C=${6:-0}
safe_id "$L" leg; safe_id "$B" batch; safe_id "$T" task
[[ "$C" =~ ^[0-9]+(\.[0-9]+)?$ ]] || { echo "ERROR: credits '$C' is not a number" >&2; exit 2; }
need_repo
case $L in perplexity-cloud|perplexity-lemonade) H=$L;; *) H=perplexity-local;; esac
R="$KW_HOME/runs/$L/$B"; A="$KW_HOME/answers/$L-$T.md"
mkdir -p "$KW_HOME/answers"
powershell -NoProfile -Command "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw" | "$PY" "$HERE/thread_answer.py" > "$A"
W=$(deliverable_seconds "$R" "$T")
(cd "$GAIA_KW_REPO" && PYTHONPATH=src "$PY" -m gaia.cli eval tasks kw record "$(winpath "$R")" "$T" \
  --answer-file "$(winpath "$A")" --wall-seconds "$W" --credits "$C" --harness "$H" --model "$M" \
  --notes "app: Finished $APP" --force >/dev/null)
echo "recorded $T: files at +${W}s, app $APP, answer $(wc -c < "$A") bytes"
head -c 300 "$A"
