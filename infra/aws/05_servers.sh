#!/usr/bin/env bash
# Commit 5 (Step 3): one EC2 server in each Availability Zone, both running the
# lookup service (commit 3) on port 8000.
#
#   server-a  in subnet A (ap-south-1a)  ┐ security group "app": reachable on
#   server-b  in subnet B (ap-south-1b)  ┘ port 8000 only from the load balancer
#
# Needs the network from 01_network.sh. Safe to run again: running servers are
# reused, not duplicated. IDs and IPs are saved to .state/servers.env.
#
#   ./infra/aws/05_servers.sh              launch both servers (or reuse them)
#   ./infra/aws/05_servers.sh logs a       boot log of server-a (or b)
#   ./infra/aws/05_servers.sh status       show both servers
#   ./infra/aws/05_servers.sh render       print the startup script without launching
set -euo pipefail
source "$(dirname "$0")/config.sh"
if [[ "${1:-up}" == "up" ]]; then
  [[ -f "$NETWORK_STATE" ]] || die "No $NETWORK_STATE. Run 01_network.sh first (commit 4)."
  source "$NETWORK_STATE"
fi

INSTANCE_TYPE="${INSTANCE_TYPE:-t3.micro}"
AMI_PARAM="${AMI_PARAM:-/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64}"
SERVERS_STATE="$STATE_DIR/servers.env"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LIVE_STATES="pending,running,stopping,stopped"

find_server() {  # find_server a|b -> instance id of a live server, or nothing
  local id
  id=$(aws ec2 describe-instances \
        --filters "Name=tag:Name,Values=$PROJECT-server-$1" "Name=instance-state-name,Values=$LIVE_STATES" \
        --query 'Reservations[].Instances[].InstanceId | [0]' --output text 2>/dev/null) || id=""
  [[ "$id" == "None" ]] && id=""
  echo "$id"
}

# Builds the user data: the template with the app embedded as base64(tar.gz).
build_user_data() {
  local tmp b64 version out
  tmp=$(mktemp -d)
  tar -czf "$tmp/app.tgz" -C "$REPO_ROOT/lookup" --exclude='__pycache__' --exclude='*.pyc' \
      app requirements.txt
  b64=$(base64 -w 76 < "$tmp/app.tgz" 2>/dev/null || base64 < "$tmp/app.tgz")
  version=$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo "unversioned")
  out="$tmp/user-data.sh"
  python3 - "$SCRIPT_DIR/user-data.sh" "$out" "$APP_PORT" "$version" "$b64" <<'PY'
import sys
src, dst, port, version, b64 = sys.argv[1:]
text = open(src).read()
for key, val in (("__APP_PORT__", port), ("__APP_VERSION__", version), ("__APP_TARBALL_B64__", b64)):
    assert key in text, key
    text = text.replace(key, val)
open(dst, "w").write(text)
PY
  local size; size=$(wc -c < "$out")
  (( size < 16000 )) || die "user data is $size bytes; EC2 allows 16 KB. Is lookup/ bigger than expected?"
  echo "$out"
}

launch() {  # launch a|b <subnet> <az> <user-data file>
  local id
  id=$(find_server "$1")
  if [[ -n "$id" ]]; then ok "server-$1 already exists: $id (reusing)" >&2; echo "$id"; return; fi
  id=$(aws ec2 run-instances \
        --image-id "$AMI_ID" --instance-type "$INSTANCE_TYPE" --count 1 \
        --subnet-id "$2" --security-group-ids "$APP_SG" \
        --user-data "file://$4" \
        --metadata-options "HttpTokens=required,HttpEndpoint=enabled" \
        --tag-specifications \
          "ResourceType=instance,Tags=[{Key=Name,Value=$PROJECT-server-$1},{Key=Project,Value=$PROJECT},{Key=Zone,Value=$3}]" \
          "ResourceType=volume,Tags=[{Key=Name,Value=$PROJECT-server-$1},{Key=Project,Value=$PROJECT}]" \
        --query 'Instances[0].InstanceId' --output text)
  ok "launched server-$1: $id in $3" >&2
  echo "$id"
}

save_state() {
  local ia="$1" ib="$2" ipa ipb
  ipa=$(aws ec2 describe-instances --instance-ids "$ia" --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
  ipb=$(aws ec2 describe-instances --instance-ids "$ib" --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
  cat > "$SERVERS_STATE" <<EOF
# Written by 05_servers.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ). Read by verify_servers.sh and commit 6.
INSTANCE_A=$ia
INSTANCE_B=$ib
IP_A=$ipa
IP_B=$ipb
EOF
}

case "${1:-up}" in
  up)
    log "Checking AWS access"; require_aws
    log "Amazon Linux 2023 image"
    AMI_ID=$(aws ssm get-parameter --name "$AMI_PARAM" --query Parameter.Value --output text) \
      || die "Could not look up the Amazon Linux 2023 image"
    ok "$AMI_ID"
    log "Packing the lookup app into the startup script"
    UD=$(build_user_data); ok "$(wc -c < "$UD") bytes (EC2 limit 16 KB)"
    log "Servers ($INSTANCE_TYPE)"
    IA=$(launch a "$SUBNET_A" "$AZ_A" "$UD")
    IB=$(launch b "$SUBNET_B" "$AZ_B" "$UD")
    rm -rf "$(dirname "$UD")"
    log "Waiting for both servers to be running"
    aws ec2 wait instance-running --instance-ids "$IA" "$IB"
    ok "running"
    save_state "$IA" "$IB"
    log "Done. Saved to ${SERVERS_STATE#$PWD/}"
    grep -v '^#' "$SERVERS_STATE" | sed 's/^/    /'
    echo
    echo "The app needs 1-3 minutes after 'running' to install and start. Then:"
    echo "    ./infra/aws/allow_my_ip.sh add       # let your computer reach port 8000"
    echo "    ./infra/aws/verify_servers.sh        # checks both servers end to end"
    echo "    ./infra/aws/allow_my_ip.sh remove    # close it again"
    ;;
  render)
    # Prints the final startup script (app embedded) without launching anything.
    UD=$(build_user_data); cat "$UD"; rm -rf "$(dirname "$UD")"
    ;;
  logs)
    which="${2:-a}"; id=$(find_server "$which")
    [[ -n "$id" ]] || die "no live server-$which"
    # The console log includes everything user-data printed. It can lag a few minutes behind.
    aws ec2 get-console-output --instance-id "$id" --latest --query Output --output text 2>/dev/null \
      | grep -E "cowin|Cloud-init|error|Error" || echo "(no output yet; try again in a minute)"
    ;;
  status)
    aws ec2 describe-instances --filters "Name=tag:Project,Values=$PROJECT" "Name=instance-state-name,Values=$LIVE_STATES" \
      --query 'Reservations[].Instances[].[Tags[?Key==`Name`]|[0].Value,InstanceId,State.Name,Placement.AvailabilityZone,PublicIpAddress]' \
      --output table
    ;;
  *) echo "usage: $0 [up | status | logs a|b | render]"; exit 1 ;;
esac
