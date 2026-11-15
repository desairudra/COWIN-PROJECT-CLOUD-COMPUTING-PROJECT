#!/usr/bin/env bash
# E1: the autoscaler under a load spike. 20 req/s, ramp to PEAK, hold, drop back, watch it scale down.
#   ./experiments/e1_hpa/run_e1.sh
# Env: PEAK=300  LOW=20  HOLD=180s  COOL=180s   (about 8 min with the defaults)
set -euo pipefail
. "$(dirname "$0")/../k8s/lib.sh"
PEAK="${PEAK:-300}"
LOW="${LOW:-20}"

need_cluster
if command -v k6 >/dev/null; then K6=(k6)
else K6=(docker run --rm -i --network host -u "$(id -u):$(id -g)" -v "$ROOT:$ROOT" -w "$ROOT" -e BASE_URL -e PEAK -e LOW -e HOLD -e COOL grafana/k6)
fi
kubectl top pods -n "$NS" >/dev/null 2>&1 || die "metrics-server has no numbers yet; wait a minute after up.sh"

new_run e1-hpa
echo "==> Back to the minimum (2 pods) before starting"
set_min_replicas 2
for _ in $(seq 60); do
  [[ $(kubectl -n "$NS" get deploy lookup -o jsonpath='{.status.replicas}') == 2 ]] && break
  sleep 5
done

cat > "$RUN/run.env" <<ENV
EXPERIMENT=E1
TITLE=E1 autoscaling: $LOW to $PEAK requests/s and back
PEAK=$PEAK
LOW=$LOW
HOLD=${HOLD:-180s}
COOL=${COOL:-180s}
URL=http://localhost:8080
HPA=$(kubectl -n "$NS" get hpa lookup -o jsonpath='min {.spec.minReplicas}, max {.spec.maxReplicas}, target {.spec.metrics[0].resource.target.averageUtilization}% of the CPU request')
ENV

echo "t_s,current,desired,cpu_pct" > "$RUN/hpa.csv"
sample_hpa() {
  while :; do
    echo "$(now),$(kubectl -n "$NS" get hpa lookup -o jsonpath='{.status.currentReplicas},{.status.desiredReplicas},{.status.currentMetrics[0].resource.current.averageUtilization}' 2>/dev/null)" >> "$RUN/hpa.csv"
    sleep 5
  done
}

echo "==> Load: $LOW -> $PEAK requests/s and back (k6), sampling the autoscaler every 5 s"
export BASE_URL=http://localhost:8080 PEAK LOW
mkdir -p "$ROOT/loadtest/.out"
raw="$ROOT/loadtest/.out/$(basename "$RUN").csv"
start_clock
sample_pods & sampler=$!
sample_hpa & hpa=$!
trap 'stop_bg $sampler $hpa' EXIT
"${K6[@]}" run --quiet --out "csv=$raw" --summary-export "$RUN/summary.json" experiments/e1_hpa/e1.js > "$RUN/k6.txt" || true
stop_bg "$sampler" "$hpa"
trap - EXIT
echo "K6_START=$T0" >> "$RUN/run.env"

python3 "$ROOT/experiments/e1_hpa/plot_e1.py" "$RUN" "$raw"
gzip -f "$raw"
