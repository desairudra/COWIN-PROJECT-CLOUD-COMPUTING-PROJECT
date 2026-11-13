#!/usr/bin/env bash
# Deletes the load balancer, its listener and target group (found by
# name). The servers and network stay. Run before 98 and 99.
#
#   ./infra/aws/97_teardown_alb.sh          # asks for confirmation
#   ./infra/aws/97_teardown_alb.sh --yes
set -euo pipefail
source "$(dirname "$0")/config.sh"
require_aws
ALB_NAME="$PROJECT-alb"; TG_NAME="$PROJECT-tg"

ALB_ARN=$(aws elbv2 describe-load-balancers --names "$ALB_NAME" \
           --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null) || ALB_ARN=""
TG_ARN=$(aws elbv2 describe-target-groups --names "$TG_NAME" \
          --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null) || TG_ARN=""
[[ "$ALB_ARN" == "None" ]] && ALB_ARN=""; [[ "$TG_ARN" == "None" ]] && TG_ARN=""
if [[ -z "$ALB_ARN" && -z "$TG_ARN" ]]; then
  ok "no load balancer or target group found; nothing to delete"; rm -f "$STATE_DIR/alb.env"; exit 0
fi

if [[ "${1:-}" != "--yes" ]]; then
  read -r -p "Delete load balancer $ALB_NAME and target group $TG_NAME? [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]] || { echo "Cancelled."; exit 0; }
fi

if [[ -n "$ALB_ARN" ]]; then
  log "Load balancer"
  aws elbv2 delete-load-balancer --load-balancer-arn "$ALB_ARN"   # also deletes its listeners
  aws elbv2 wait load-balancers-deleted --load-balancer-arns "$ALB_ARN"
  ok "deleted $ALB_NAME"
fi
if [[ -n "$TG_ARN" ]]; then
  log "Target group"
  # A target group can only be deleted once no load balancer uses it; retry briefly.
  for i in 1 2 3 4 5 6; do
    aws elbv2 delete-target-group --target-group-arn "$TG_ARN" 2>/dev/null && { ok "deleted $TG_NAME"; break; }
    sleep 5
    (( i == 6 )) && die "could not delete $TG_NAME; try again in a minute"
  done
fi
rm -f "$STATE_DIR/alb.env"
log "Load balancer torn down. Note: its network interfaces can take a few minutes to disappear before 99_teardown_network.sh will run."
