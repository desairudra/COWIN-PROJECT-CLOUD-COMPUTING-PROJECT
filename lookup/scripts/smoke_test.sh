#!/usr/bin/env bash
# Quick check that a running lookup service answers correctly.
#   ./lookup/scripts/smoke_test.sh                         # http://localhost:8000
#   ./lookup/scripts/smoke_test.sh http://<server-ip>:8000 # one EC2 server (commit 5)
#   ./lookup/scripts/smoke_test.sh http://<alb-dns-name>   # through the load balancer (commit 6)
set -euo pipefail
BASE="${1:-http://localhost:8000}"
fail=0

check() {   # check <description> <path> <expected status>
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$BASE$2" || echo "000")
  if [[ "$code" == "$3" ]]; then echo "ok    $1 ($code)"; else echo "FAIL  $1: got $code, want $3"; fail=1; fi
}

check "health"                 "/health"                      200
check "all centres"            "/centers"                     200
check "filter by city"         "/centers?city=Mumbai"         200
check "filter by pincode"      "/centers?pincode=500034"      200
check "one centre"             "/centers/MUM-001"             200
check "unknown centre is 404"  "/centers/MUM-999"             404
check "bad pincode is 422"     "/centers?pincode=12"          422

zone=$(curl -s -D - -o /dev/null --max-time 5 "$BASE/health" | tr -d '\r' \
       | awk -F': ' 'tolower($1)=="x-availability-zone"{print $2}')
echo "answered from zone: ${zone:-unknown}"
exit $fail
