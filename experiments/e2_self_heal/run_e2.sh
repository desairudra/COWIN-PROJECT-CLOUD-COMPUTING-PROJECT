#!/usr/bin/env bash
# E2: does the service keep answering while Kubernetes replaces what we break?
#   ./experiments/e2_self_heal/run_e2.sh pods   # force-kill a pod at 30, 60 and 90 s
#   ./experiments/e2_self_heal/run_e2.sh zone   # stop the whole zone a node at 40 s, start it at 180 s
# Env: RATE=20 (probe requests/s)  PODS=4
set -euo pipefail
. "$(dirname "$0")/../k8s/lib.sh"
MODE="${1:-pods}"
RATE="${RATE:-20}"
PODS="${PODS:-4}"
case "$MODE" in
  pods) DURATION=150; DOOR=8080 ;;
  zone) DURATION=260; DOOR=8081 ;;
  *) die "mode is pods or zone" ;;
esac

need_cluster
new_run "e2-$MODE"
ZONE_A_NODE=$(kubectl get nodes -l topology.kubernetes.io/zone=ap-south-1a -o jsonpath='{.items[0].metadata.name}')

echo "==> $PODS pods, spread over both zones"
set_min_replicas "$PODS"
wait_ready "$PODS"
kubectl -n "$NS" get pods -l app=lookup -o wide --no-headers | awk '{print "    "$1"  "$7}'

cat > "$RUN/run.env" <<ENV
EXPERIMENT=E2
MODE=$MODE
TITLE=E2 self-healing: $( [[ $MODE == pods ]] && echo "pods killed" || echo "zone a node stopped" )
GROUP=node
RATE=$RATE
DURATION=$DURATION
PODS=$PODS
URL=http://localhost:$DOOR/centers?city=Mumbai
ZONE_A_NODE=$ZONE_A_NODE
ENV

echo "==> Probing $RATE requests/s for $DURATION s through the door on :$DOOR"
start_clock
python3 "$PROBE" "http://localhost:$DOOR/centers?city=Mumbai" "$RUN/probe.csv" --rate "$RATE" --duration "$DURATION" --timeout 2 &
probe=$!
sample_pods & sampler=$!
trap 'stop_bg $probe $sampler; [[ $MODE == zone ]] && docker start "$ZONE_A_NODE" >/dev/null 2>&1; set_min_replicas 2' EXIT

at() { while (( $(awk -v n="$(now)" -v t="$1" 'BEGIN{print (n<t)}') )); do sleep 0.2; done; }

if [[ $MODE == pods ]]; then
  for t in 30 60 90; do
    at "$t"
    pod=$(kubectl -n "$NS" get pods -l app=lookup --field-selector=status.phase=Running -o jsonpath='{.items[*].metadata.name}' | tr ' ' '\n' | shuf -n1)
    kubectl -n "$NS" delete pod "$pod" --grace-period=0 --force >/dev/null 2>&1 || true
    event "killed $pod"
  done
else
  at 40
  docker stop "$ZONE_A_NODE" >/dev/null
  event "zone a node stopped"
  at 180
  docker start "$ZONE_A_NODE" >/dev/null
  event "zone a node started"
fi

wait "$probe"
stop_bg "$sampler"
trap - EXIT
[[ $MODE == zone ]] && { kubectl wait --for=condition=Ready "node/$ZONE_A_NODE" --timeout=120s >/dev/null || true; }
set_min_replicas 2

python3 "$ROOT/experiments/k8s/analyze.py" "$RUN"
