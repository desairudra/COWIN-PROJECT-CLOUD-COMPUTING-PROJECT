#!/usr/bin/env bash
# E3: release v2 while users are using the site.
#   ./experiments/e3_rollout/run_e3.sh rolling    # one new pod at a time, old ones leave only when new ones are ready
#   ./experiments/e3_rollout/run_e3.sh recreate   # stop all old pods, then start the new ones
# Env: RATE=20  PODS=4
set -euo pipefail
. "$(dirname "$0")/../k8s/lib.sh"
MODE="${1:-rolling}"
RATE="${RATE:-20}"
PODS="${PODS:-4}"
DURATION=100
case "$MODE" in
  rolling) STRATEGY='{"spec":{"strategy":{"type":"RollingUpdate","rollingUpdate":{"maxSurge":1,"maxUnavailable":0}}}}' ;;
  recreate) STRATEGY='{"spec":{"strategy":{"type":"Recreate","rollingUpdate":null}}}' ;;
  *) die "mode is rolling or recreate" ;;
esac

need_cluster
new_run "e3-$MODE"

echo "==> $PODS pods on release v1, strategy $MODE"
set_min_replicas "$PODS"
kubectl -n "$NS" patch deploy lookup --type=merge -p "$STRATEGY" >/dev/null
kubectl -n "$NS" set env deploy/lookup APP_RELEASE=v1 >/dev/null
kubectl -n "$NS" rollout status deploy/lookup --timeout=180s >/dev/null
wait_ready "$PODS"

cat > "$RUN/run.env" <<ENV
EXPERIMENT=E3
MODE=$MODE
TITLE=E3 release v1 to v2: $MODE
GROUP=release
RATE=$RATE
DURATION=$DURATION
PODS=$PODS
URL=http://localhost:8080/centers?city=Mumbai
ENV

reset() {
  kubectl -n "$NS" patch deploy lookup --type=merge \
    -p '{"spec":{"strategy":{"type":"RollingUpdate","rollingUpdate":{"maxSurge":1,"maxUnavailable":0}}}}' >/dev/null
  kubectl -n "$NS" set env deploy/lookup APP_RELEASE=v1 >/dev/null
  set_min_replicas 2
}

echo "==> Probing $RATE requests/s for $DURATION s; v2 goes out at 20 s"
start_clock
python3 "$PROBE" "http://localhost:8080/centers?city=Mumbai" "$RUN/probe.csv" --rate "$RATE" --duration "$DURATION" --timeout 2 &
probe=$!
sample_pods & sampler=$!
trap 'stop_bg $probe $sampler; reset' EXIT

while (( $(awk -v n="$(now)" 'BEGIN{print (n<20)}') )); do sleep 0.2; done
kubectl -n "$NS" set env deploy/lookup APP_RELEASE=v2 >/dev/null
event "rollout of v2 started"
kubectl -n "$NS" rollout status deploy/lookup --timeout=120s >/dev/null && event "rollout finished"

wait "$probe"
stop_bg "$sampler"
trap - EXIT
reset
echo "==> Back on v1 (rolling) for the next run"

python3 "$ROOT/experiments/k8s/analyze.py" "$RUN"
