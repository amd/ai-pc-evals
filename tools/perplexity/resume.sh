#!/bin/bash
# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
# usage: resume.sh <leg> <batch> <task> — like go.sh but keeps the existing .start mark
# (records to new .resume-<time> files so the first recording and resource log are kept)
set -uo pipefail
. "$(dirname "$0")/_env.sh"
[ $# -eq 3 ] || { echo "usage: resume.sh <leg> <batch> <task>" >&2; exit 2; }
L=$1; B=$2; T=$3
safe_id "$L" leg; safe_id "$B" batch; safe_id "$T" task
R="$KW_HOME/runs/$L/$B"; N=$(date +%s); V="$KW_HOME/recordings/$L-$T.resume-$N.mp4"; M="$KW_HOME/sysmon/$L-$T.resume-$N.csv"
[ -f "$R/.start-$T" ] || { echo "ERROR: no start mark $R/.start-$T; use go.sh" >&2; exit 1; }
mkdir -p "$KW_HOME/recordings" "$KW_HOME/sysmon"
("$PY" "$HERE/rec.py" "$V" >"$V.log" 2>&1 &)
(powershell -NoProfile -ExecutionPolicy Bypass -File "$(winpath "$HERE/sysmon.ps1")" "$(winpath "$M")" >"$M.log" 2>&1 &)
"$PY" "$HERE/wait_task.py" "$L" "$B" "$T"; rc=$?
touch "$V.stop" "$M.stop"
exit $rc
