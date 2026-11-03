#!/bin/bash
# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
# usage: rec_thread.sh <leg> <batch> <task> "<app time>" <model> [credits]  — record from a copied thread (clipboard)
# Optional: KW_MODE, KW_REASONING, KW_SPEED, KW_APP_VERSION, KW_QUEUE_SECONDS, KW_LOCAL_TOKENS, KW_ENGINE_BUILD and
# KW_OUTCOME (stalled-asked-human, did-not-run) are passed to kwengine record (see _env.sh).
# Cloud runs: pass the credits the app showed. The harness is perplexity-cloud for the cloud leg,
# perplexity-lemonade for the lemonade leg, perplexity-local otherwise.
set -euo pipefail
. "$(dirname "$0")/_env.sh"
[ $# -ge 5 ] || { echo 'usage: rec_thread.sh <leg> <batch> <task> "<app time>" <model> [credits]' >&2; exit 2; }
L=$1; B=$2; T=$3; APP=$4; M=$5; C=${6:-0}
safe_id "$L" leg; safe_id "$B" batch; safe_id "$T" task
[[ "$C" =~ ^[0-9]+(\.[0-9]+)?$ ]] || { echo "ERROR: credits '$C' is not a number" >&2; exit 2; }
need_engine
case $L in perplexity-cloud|perplexity-lemonade) H=$L;; *) H=perplexity-local;; esac
R="$KW_HOME/runs/$L/$B"; A="$KW_HOME/answers/$L-$T.md"
mkdir -p "$KW_HOME/answers"
# The prompt marks where the answer starts in a cloud thread, which has no agent time line.
P=$(mktemp --suffix=.txt); trap 'rm -f "$P"' EXIT
"$PY" "$HERE/prompt.py" "$L" "$B" "$T" > "$P"
powershell -NoProfile -Command "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw" \
  | "$PY" "$HERE/thread_answer.py" --prompt-file "$(winpath "$P")" > "$A"
W=$(deliverable_seconds "$R" "$T")
run_info_args
# A wait behind another session is not the agent's time: it is recorded apart, as --queue-seconds.
Q=${KW_QUEUE_SECONDS:-0}; Q=${Q%.*}; W=$(( W > Q ? W - Q : 0 ))
kw record "$(winpath "$R")" "$T" \
  --answer-file "$(winpath "$A")" --wall-seconds "$W" --credits "$C" --harness "$H" --model "$M" \
  --notes "app: Finished $APP" "${RUN_INFO[@]}" --force >/dev/null
echo "recorded $T: files at +${W}s, app $APP, answer $(wc -c < "$A") bytes"
head -c 300 "$A"
