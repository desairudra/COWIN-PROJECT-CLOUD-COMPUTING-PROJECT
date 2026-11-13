#!/usr/bin/env bash
# Checks commit 5: two running servers, one per AZ, locked down, each serving
# the lookup app and reporting its own zone. Prints PASS/FAIL per check.
#
# The HTTP checks need your IP allowed on port 8000 first:
#   ./infra/aws/allow_my_ip.sh add
#   ./infra/aws/verify_servers.sh | tee docs/evidence/05-verify-servers.txt
#   ./infra/aws/allow_my_ip.sh remove
#
# SKIP_HTTP=1 runs only the AWS-side checks.
set -uo pipefail
source "$(dirname "$0")/config.sh"
[[ -f "$NETWORK_STATE" ]] || die "No $NETWORK_STATE. Run 01_network.sh first."
SERVERS_STATE="$STATE_DIR/servers.env"
[[ -f "$SERVERS_STATE" ]] || die "No $SERVERS_STATE. Run 05_servers.sh first."
source "$NETWORK_STATE"; source "$SERVERS_STATE"

fails=0
check() {
  if [[ "$2" == "$3" ]]; then printf '  PASS  %s\n' "$1"
  else printf '  FAIL  %s (got "%s", want "%s")\n' "$1" "$2" "$3"; fails=$((fails + 1)); fi
}
inst() { aws ec2 describe-instances --instance-ids "$1" --query "Reservations[0].Instances[0].$2" --output text 2>/dev/null || echo error; }

echo "Server check for $PROJECT in $AWS_REGION ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
for s in A B; do
  l=$(echo "$s" | tr AB ab)
  id_var="INSTANCE_$s"; sub_var="SUBNET_$s"; az_var="AZ_$s"
  id="${!id_var}"
  check "server-$l ($id) is running"          "$(inst "$id" State.Name)" "running"
  check "server-$l is in ${!az_var}"           "$(inst "$id" Placement.AvailabilityZone)" "${!az_var}"
  check "server-$l is in subnet ${s}"          "$(inst "$id" SubnetId)" "${!sub_var}"
  check "server-$l uses only the app SG"       "$(inst "$id" 'SecurityGroups[*].GroupId | join(`,`, @)')" "$APP_SG"
  check "server-$l requires IMDSv2"            "$(inst "$id" MetadataOptions.HttpTokens)" "required"
done
check "the two servers are in different AZs" \
  "$([[ "$(inst "$INSTANCE_A" Placement.AvailabilityZone)" != "$(inst "$INSTANCE_B" Placement.AvailabilityZone)" ]] && echo yes || echo no)" "yes"

if [[ -z "${SKIP_HTTP:-}" ]]; then
  echo
  echo "HTTP checks (need ./infra/aws/allow_my_ip.sh add):"
  for s in A B; do
    l=$(echo "$s" | tr AB ab)
    ip_var="IP_$s"; az_var="AZ_$s"; id_var="INSTANCE_$s"; ip="${!ip_var}"
    body=$(curl -s --max-time 5 "http://$ip:$APP_PORT/health" || true)
    if [[ -z "$body" ]]; then
      printf '  FAIL  server-%s (%s) did not answer on port %s. Allowed your IP? App still booting? (05_servers.sh logs %s)\n' \
        "$l" "$ip" "$APP_PORT" "$l"
      fails=$((fails + 1)); continue
    fi
    zone=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["served_by"]["availability_zone"])' "$body" 2>/dev/null || echo "?")
    iid=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["served_by"]["instance_id"])' "$body" 2>/dev/null || echo "?")
    check "server-$l /health answers"                     "$(echo "$body" | grep -c '"status":"ok"')" "1"
    check "server-$l reports its own zone (${!az_var})"   "$zone" "${!az_var}"
    check "server-$l reports its own instance ID"         "$iid" "${!id_var}"
    count=$(curl -s --max-time 5 "http://$ip:$APP_PORT/centers" | python3 -c 'import json,sys; print(json.load(sys.stdin)["count"])' 2>/dev/null || echo "?")
    check "server-$l serves all 20 centres"               "$count" "20"
  done
fi

echo
if (( fails == 0 )); then echo "All checks passed."; else echo "$fails check(s) failed."; fi
exit $(( fails > 0 ))
