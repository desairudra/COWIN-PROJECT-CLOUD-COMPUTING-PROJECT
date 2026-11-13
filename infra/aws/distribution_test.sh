#!/usr/bin/env bash
# Sends N requests (default 200) through the load balancer and counts which
# Availability Zone answered each one, using the X-Availability-Zone header the
# lookup service adds . Shows the load balancer spreads traffic evenly.
#
#   ./infra/aws/distribution_test.sh | tee docs/evidence/06-distribution.txt
#   ./infra/aws/distribution_test.sh 500
#   URL=http://localhost:8080 ./infra/aws/distribution_test.sh   # any URL
#
# Exits non-zero if any request fails, or if one zone gets under 35% of traffic.
set -uo pipefail
source "$(dirname "$0")/config.sh"
N="${1:-200}"
if [[ -z "${URL:-}" ]]; then
  [[ -f "$STATE_DIR/alb.env" ]] || die "No $STATE_DIR/alb.env. Run 06_load_balancer.sh first."
  source "$STATE_DIR/alb.env"
  URL="http://$ALB_DNS"
fi
PATH_="${REQ_PATH:-/centers}"
tmp=$(mktemp)

echo "Distribution test: $N requests to $URL$PATH_ ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
start=$(python3 -c "import time; print(time.time())")
for i in $(seq 1 "$N"); do
  # status code + zone header, one line per request
  curl -s -o /dev/null --max-time 5 -D - -w 'STATUS %{http_code}\n' "$URL$PATH_" \
    | tr -d '\r' | awk -F': ' '
        tolower($1)=="x-availability-zone" {zone=$2}
        /^STATUS / {print (zone ? zone : "none"), substr($0, 8)}' >> "$tmp"
  (( i % 50 == 0 )) && echo "    sent $i"
done
elapsed=$(python3 -c "import time; print(time.time() - $start)")

python3 - "$tmp" "$N" "$elapsed" <<'PY'
import collections, sys
path, n, elapsed = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
rows = [l.split() for l in open(path) if l.strip()]
ok = [z for z, s in rows if s == "200"]
failed = [(z, s) for z, s in rows if s != "200"]
zones = collections.Counter(ok)
print()
print(f"{'Availability Zone':<20}{'requests':>10}{'share':>10}")
for zone, count in sorted(zones.items()):
    print(f"{zone:<20}{count:>10}{count / n:>10.1%}")
print(f"{'failed (non-200)':<20}{len(failed):>10}{len(failed) / n:>10.1%}")
print(f"\n{n} requests in {elapsed:.1f} s ({n / elapsed:.0f} requests/s, one at a time)")
problems = []
if failed:
    problems.append(f"{len(failed)} request(s) failed: {collections.Counter(s for _, s in failed)}")
if len(zones) < 2:
    problems.append(f"only {len(zones)} zone(s) answered; expected 2")
elif min(zones.values()) / n < 0.35:
    problems.append("traffic is uneven: one zone got under 35%")
if problems:
    print("RESULT: FAIL - " + "; ".join(problems))
    sys.exit(1)
print("RESULT: PASS - traffic is spread across both zones")
PY
rc=$?
rm -f "$tmp"
exit $rc
