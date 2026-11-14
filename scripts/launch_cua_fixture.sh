#!/usr/bin/env bash
# Launch a bounded synthetic evaluation independent of SSH and user systemd.
set -euo pipefail
eval_root=${1:?Usage: launch_cua_fixture.sh EVAL_ROOT RUN_NAME lcu|cua-driver}
run_name=${2:?run name required}
backend=${3:?backend required}
[[ "$eval_root" = /* && -d "$eval_root" ]] || exit 2
[[ "$run_name" =~ ^[a-zA-Z0-9_-]+$ ]] || exit 2
[[ "$backend" = lcu || "$backend" = cua-driver ]] || exit 2
[[ ! -e "$eval_root/$run_name" && ! -e "$eval_root/$run_name.launch.log" ]] || exit 2
eval_runtime=$(mktemp -d /tmp/lcu-eval-runtime.XXXXXX)
eval_node=${LCU_NODE_PATH:?Set LCU_NODE_PATH to your installed runtime node binary}
[[ -x "$eval_node" ]] || exit 2
nohup setsid --fork --wait timeout --signal=TERM --kill-after=10s 900 \
  env XDG_RUNTIME_DIR="$eval_runtime" \
  xvfb-run -a -s '-screen 0 1440x1000x24 -nolisten tcp' dbus-run-session -- \
  "$eval_node" "$eval_root/lcu_glm_smoke.mjs" \
  --lcu "$eval_root/runtime-new/current" --backend "$backend" \
  --python "$eval_root/baseline-venv/bin/python" --explicit-atspi \
  --output "$eval_root/$run_name" \
  > "$eval_root/$run_name.launch.log" 2>&1 < /dev/null &
printf 'monitor_pid=%s\noutput=%s\nlog=%s\nruntime=%s\n' \
  "$!" "$eval_root/$run_name" "$eval_root/$run_name.launch.log" "$eval_runtime"
