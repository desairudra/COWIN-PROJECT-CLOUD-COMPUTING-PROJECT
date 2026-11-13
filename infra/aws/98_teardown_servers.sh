#!/usr/bin/env bash
# Terminates the commit 5 servers (found by tag) and waits until they are gone,
# so 99_teardown_network.sh can run straight after.
#
#   ./infra/aws/98_teardown_servers.sh          # asks for confirmation
#   ./infra/aws/98_teardown_servers.sh --yes
set -euo pipefail
source "$(dirname "$0")/config.sh"
require_aws

ids=$(aws ec2 describe-instances \
       --filters "Name=tag:Project,Values=$PROJECT" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
       --query 'Reservations[].Instances[].InstanceId' --output text)
if [[ -z "$ids" || "$ids" == "None" ]]; then
  ok "no $PROJECT servers running"; rm -f "$STATE_DIR/servers.env"; exit 0
fi
echo "    servers: $ids"
if [[ "${1:-}" != "--yes" ]]; then
  read -r -p "Terminate these servers? [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]] || { echo "Cancelled."; exit 0; }
fi
# shellcheck disable=SC2086
aws ec2 terminate-instances --instance-ids $ids >/dev/null
log "Waiting for termination (about a minute)"
# shellcheck disable=SC2086
aws ec2 wait instance-terminated --instance-ids $ids
ok "terminated $ids"
rm -f "$STATE_DIR/servers.env"
