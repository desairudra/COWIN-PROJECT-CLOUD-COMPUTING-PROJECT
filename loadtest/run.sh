#!/usr/bin/env bash
# Step load test with k6, then results.md + chart in docs/evidence/.
#   ./loadtest/run.sh lookup                       # through the load balancer (infra/aws/.state/alb.env)
#   ./loadtest/run.sh lookup http://localhost:8000
#   ./loadtest/run.sh registration                 # http://localhost:8000, needs TRUST_PROXY=1
# Env: RATES=25,50,100 (one step per rate)  STEP=60 (seconds per step)  P95_LIMIT=500 (ms)
#      CHECK=1 exits 1 unless the first step passed (used by CI)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

target="${1:-}"
case "$target" in
  lookup)
    if [[ -n "${2:-}" ]]; then url="$2"
    elif [[ -f infra/aws/.state/alb.env ]]; then url="http://$(. infra/aws/.state/alb.env; echo "$ALB_DNS")"
    else url="http://localhost:8000"; fi
    rates="${RATES:-25,50,100,200,400}"; unit="requests/s"
    title="Lookup service load test"
    probe="$url/health" ;;
  registration)
    url="${2:-http://localhost:8000}"
    rates="${RATES:-2,5,10,20,40}"; unit="sign-ups/s"
    title="Registration load test (OTP, verify, add, list)"
    probe="$url/healthz" ;;
  *) sed -n '2,6p' "$0"; exit 2 ;;
esac
step="${STEP:-60}"
url="${url%/}"

if ! curl -sf --max-time 5 -o /dev/null "$probe"; then
  echo "Nothing answering at $probe. Start the service (or build the AWS setup) first." >&2
  exit 1
fi

if command -v k6 >/dev/null; then
  K6=(k6)
elif command -v docker >/dev/null; then
  echo "k6 not installed, using the grafana/k6 Docker image"
  K6=(docker run --rm -i --network host -u "$(id -u):$(id -g)" -v "$ROOT:$ROOT" -w "$ROOT"
      -e RATES -e STEP -e BASE_URL grafana/k6)
else
  echo "Install k6 (https://grafana.com/docs/k6/latest/set-up/install-k6/) or Docker." >&2
  exit 1
fi

stamp="$(date -u +%Y%m%d-%H%M%S)"
when="$(date -u '+%Y-%m-%d %H:%M')"
out="docs/evidence/13-load-$target-$stamp"
raw="loadtest/.out/$target-$stamp"
mkdir -p "$out" "$raw"

IFS=, read -ra list <<< "$rates"
echo "==> $title"
echo "    target $url, steps ${rates//,/ \/ } $unit, $step s each (about $(( ${#list[@]} * step / 60 + 1 )) min)"

export RATES="$rates" STEP="$step" BASE_URL="$url"
t_start="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
"${K6[@]}" run --quiet --out "csv=$raw/raw.csv" --summary-export "$raw/summary.json" \
  "loadtest/k6/$target.js" | tee "$raw/k6.txt"
t_end="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

{
  echo "TARGET=$target"; echo "URL=$url"; echo "RATES=$rates"; echo "STEP=$step"
  echo "START=$t_start"; echo "END=$t_end"; echo "K6=$("${K6[@]}" version 2>/dev/null | head -1)"
  echo "GIT=$(git rev-parse --short HEAD 2>/dev/null || echo none)"
} > "$out/run.env"

status=0
python3 loadtest/analyze.py --raw "$raw/raw.csv" --summary "$raw/summary.json" --out "$out" \
  --unit "$unit" --step "$step" --p95-limit "${P95_LIMIT:-500}" --title "$title" --target-url "$url" \
  ${CHECK:+--check} --note "Run $when UTC, steps of $step s at ${rates//,/, } $unit." || status=$?
gzip -f "$raw/raw.csv"
echo "Raw k6 output (not committed): $raw/raw.csv.gz"
if [[ "$url" == *.elb.amazonaws.com* ]]; then
  echo "CloudWatch graphs of this run (wait 3 min for the metrics):"
  echo "  ./infra/aws/export_graphs.sh --start $t_start --end $t_end --out $out"
fi
exit $status
