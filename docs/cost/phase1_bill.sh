#!/usr/bin/env bash
# What our own AWS work cost, per service, from Cost Explorer (report Sec 6.6).
#   ./docs/cost/phase1_bill.sh                    from 1 Nov 2026 to today
#   START=2026-11-10 ./docs/cost/phase1_bill.sh
# Needs the IAM permission ce:GetCostAndUsage. Each call costs $0.01.
# Writes docs/evidence/17-bill-<date>.json; then run python docs/cost/cost_model.py.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
START="${START:-2026-11-01}"
END="$(date -u -d tomorrow +%F 2>/dev/null || date -u -v+1d +%F)"
FILE="17-bill-$(date -u +%Y%m%d).json"
OUT="$ROOT/docs/evidence/$FILE"

raw=$(aws ce get-cost-and-usage --region us-east-1 \
  --time-period "Start=$START,End=$END" --granularity MONTHLY --metrics UnblendedCost \
  --group-by Type=DIMENSION,Key=SERVICE --output json) || {
  echo "Cost Explorer refused. Add ce:GetCostAndUsage to the IAM user (an inline policy, like the" >&2
  echo "CloudFormation one), or read the bill in the console: Billing and Cost Management -> Bills." >&2
  exit 1
}

RAW="$raw" python3 - "$START" "$END" "$FILE" "$OUT" <<'PY'
import json, os, sys
from collections import defaultdict
start, end, name, out = sys.argv[1:]
data = json.loads(os.environ["RAW"])
per = defaultdict(float)
for period in data["ResultsByTime"]:
    for g in period["Groups"]:
        per[g["Keys"][0]] += float(g["Metrics"]["UnblendedCost"]["Amount"])
services = [{"service": k, "usd": round(v, 4)} for k, v in sorted(per.items(), key=lambda x: -x[1])]
json.dump({"start": start, "end": end, "file": name, "services": services}, open(out, "w"), indent=1)
total = sum(s["usd"] for s in services)
for s in services:
    if s["usd"] >= 0.005:
        print(f"  {s['usd']:9.2f}  {s['service']}")
print(f"  {total:9.2f}  TOTAL (USD, {start} to {end})")
print(f"Saved to docs/evidence/{name}")
PY
