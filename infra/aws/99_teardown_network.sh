#!/usr/bin/env bash
# Deletes everything 01_network.sh created, in reverse order. Resources are
# found by their Name tags, so this works even if .state/network.env is lost.
#
# Tear down the load balancer (commit 6) and EC2 servers (commit 5) FIRST:
# AWS refuses to delete a VPC or security group that something still uses.
# This script checks and stops with a clear message if anything is left.
#
#   ./infra/aws/99_teardown_network.sh          # asks for confirmation
#   ./infra/aws/99_teardown_network.sh --yes    # no prompt
set -euo pipefail
source "$(dirname "$0")/config.sh"
require_aws

VPC_ID=$(find_by_name describe-vpcs 'Vpcs[0].VpcId' vpc)
[[ -n "$VPC_ID" ]] || { ok "no $PROJECT-vpc found in $AWS_REGION; nothing to delete"; rm -f "$NETWORK_STATE"; exit 0; }

# Refuse while anything still lives in the VPC: servers that are not fully
# terminated (commit 5), or network interfaces not attached to a server, which
# is what a load balancer uses (commit 6).
servers=$(aws ec2 describe-instances --filters "Name=vpc-id,Values=$VPC_ID" \
  "Name=instance-state-name,Values=pending,running,stopping,stopped,shutting-down" \
  --query 'Reservations[].Instances[].[InstanceId,State.Name,Placement.AvailabilityZone]' --output text)
others=$(aws ec2 describe-network-interfaces --filters "Name=vpc-id,Values=$VPC_ID" \
  --query 'NetworkInterfaces[?Attachment.InstanceId==null].[NetworkInterfaceId,Description]' --output text)
if [[ -n "$servers" && "$servers" != "None" ]] || [[ -n "$others" && "$others" != "None" ]]; then
  [[ -n "$servers" && "$servers" != "None" ]] && { echo "    servers still in the VPC:"; echo "$servers" | sed 's/^/      /'; }
  [[ -n "$others"  && "$others"  != "None" ]] && { echo "    other network interfaces (load balancer?):"; echo "$others" | sed 's/^/      /'; }
  die "Tear down the load balancer (commit 6) and servers (commit 5) first, then rerun. Servers that are 'shutting-down' need about a minute."
fi

if [[ "${1:-}" != "--yes" ]]; then
  read -r -p "Delete $PROJECT network ($VPC_ID) in $AWS_REGION? [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]] || { echo "Cancelled."; exit 0; }
fi

log "Security groups"
for name in app alb; do   # app first: it references alb
  sg=$(aws ec2 describe-security-groups --filters "Name=vpc-id,Values=$VPC_ID" "Name=group-name,Values=$PROJECT-$name" \
       --query 'SecurityGroups[0].GroupId' --output text)
  [[ "$sg" == "None" || -z "$sg" ]] && continue
  # Drop rules first so the two groups no longer reference each other.
  perms=$(aws ec2 describe-security-groups --group-ids "$sg" --query 'SecurityGroups[0].IpPermissions' --output json)
  [[ "$perms" != "[]" ]] && aws ec2 revoke-security-group-ingress --group-id "$sg" --ip-permissions "$perms" >/dev/null
done
for name in app alb; do
  sg=$(aws ec2 describe-security-groups --filters "Name=vpc-id,Values=$VPC_ID" "Name=group-name,Values=$PROJECT-$name" \
       --query 'SecurityGroups[0].GroupId' --output text)
  [[ "$sg" == "None" || -z "$sg" ]] && continue
  aws ec2 delete-security-group --group-id "$sg"; ok "deleted $sg ($name)"
done

log "Route table"
RTB_ID=$(find_by_name describe-route-tables 'RouteTables[0].RouteTableId' public-rt)
if [[ -n "$RTB_ID" ]]; then
  for assoc in $(aws ec2 describe-route-tables --route-table-ids "$RTB_ID" \
                 --query 'RouteTables[0].Associations[?!Main].RouteTableAssociationId' --output text); do
    aws ec2 disassociate-route-table --association-id "$assoc"
  done
  aws ec2 delete-route-table --route-table-id "$RTB_ID"; ok "deleted $RTB_ID"
fi

log "Subnets"
for s in subnet-a subnet-b; do
  id=$(find_by_name describe-subnets 'Subnets[0].SubnetId' "$s")
  [[ -n "$id" ]] && { aws ec2 delete-subnet --subnet-id "$id"; ok "deleted $id ($s)"; }
done

log "Internet gateway"
IGW_ID=$(find_by_name describe-internet-gateways 'InternetGateways[0].InternetGatewayId' igw)
if [[ -n "$IGW_ID" ]]; then
  aws ec2 detach-internet-gateway --internet-gateway-id "$IGW_ID" --vpc-id "$VPC_ID" 2>/dev/null || true
  aws ec2 delete-internet-gateway --internet-gateway-id "$IGW_ID"; ok "deleted $IGW_ID"
fi

log "VPC"
aws ec2 delete-vpc --vpc-id "$VPC_ID"; ok "deleted $VPC_ID"
rm -f "$NETWORK_STATE"
log "Network torn down."
