#!/usr/bin/env bash
# An Application Load Balancer in front of both servers.
#
#                     internet, port 80
#                            │
#        ALB "cowin-it457-alb" (in subnet A and subnet B, security group "alb")
#                            │  listener :80 -> forward to target group
#        target group "cowin-it457-tg"  (HTTP :8000, health check GET /health)
#               ┌────────────┴────────────┐
#        server-a (ap-south-1a)    server-b (ap-south-1b)
#
# Health check tuned for a fast failover demo (commit 8):
#   every 5 s, 2 s timeout, unhealthy after 2 failures, healthy after 2 passes,
#   so a dead server stops getting traffic within about 10 s;
#   deregistration delay 10 s (AWS default is 300 s).
#
# Safe to run again: existing pieces are reused; if the servers were replaced,
# the target group is updated to the current ones. IDs go to .state/alb.env.
#
#   ./infra/aws/06_load_balancer.sh
set -euo pipefail
source "$(dirname "$0")/config.sh"
[[ -f "$NETWORK_STATE" ]] || die "No $NETWORK_STATE. Run 01_network.sh first (commit 4)."
SERVERS_STATE="$STATE_DIR/servers.env"
[[ -f "$SERVERS_STATE" ]] || die "No $SERVERS_STATE. Run 05_servers.sh first (commit 5)."
source "$NETWORK_STATE"; source "$SERVERS_STATE"
ALB_STATE="$STATE_DIR/alb.env"

ALB_NAME="$PROJECT-alb"
TG_NAME="$PROJECT-tg"
HC_PATH="/health"

log "Checking AWS access"; require_aws

# ---- Target group -------------------------------------------------------------
log "Target group $TG_NAME"
TG_ARN=$(aws elbv2 describe-target-groups --names "$TG_NAME" \
          --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null) || TG_ARN=""
if [[ -z "$TG_ARN" || "$TG_ARN" == "None" ]]; then
  TG_ARN=$(aws elbv2 create-target-group --name "$TG_NAME" \
            --protocol HTTP --port "$APP_PORT" --vpc-id "$VPC_ID" --target-type instance \
            --health-check-protocol HTTP --health-check-path "$HC_PATH" --health-check-port traffic-port \
            --health-check-interval-seconds 5 --health-check-timeout-seconds 2 \
            --healthy-threshold-count 2 --unhealthy-threshold-count 2 --matcher HttpCode=200 \
            --tags "Key=Name,Value=$TG_NAME" "Key=Project,Value=$PROJECT" \
            --query 'TargetGroups[0].TargetGroupArn' --output text)
  ok "created"
else
  ok "reusing"
fi
aws elbv2 modify-target-group-attributes --target-group-arn "$TG_ARN" \
  --attributes Key=deregistration_delay.timeout_seconds,Value=10 >/dev/null
ok "health check $HC_PATH every 5 s, unhealthy after 2 failures; deregistration delay 10 s"

# Register the current servers; drop any old ones (e.g. after 05_servers.sh replaced them).
current=$(aws elbv2 describe-target-health --target-group-arn "$TG_ARN" \
           --query 'TargetHealthDescriptions[].Target.Id' --output text)
for old in $current; do
  if [[ "$old" != "$INSTANCE_A" && "$old" != "$INSTANCE_B" ]]; then
    aws elbv2 deregister-targets --target-group-arn "$TG_ARN" --targets "Id=$old"
    warn "removed old target $old"
  fi
done
aws elbv2 register-targets --target-group-arn "$TG_ARN" --targets "Id=$INSTANCE_A" "Id=$INSTANCE_B"
ok "targets: server-a $INSTANCE_A ($AZ_A), server-b $INSTANCE_B ($AZ_B)"

# ---- Load balancer ---------------------------------------------------------------
log "Load balancer $ALB_NAME"
ALB_ARN=$(aws elbv2 describe-load-balancers --names "$ALB_NAME" \
           --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null) || ALB_ARN=""
if [[ -z "$ALB_ARN" || "$ALB_ARN" == "None" ]]; then
  ALB_ARN=$(aws elbv2 create-load-balancer --name "$ALB_NAME" --type application \
             --scheme internet-facing --ip-address-type ipv4 \
             --subnets "$SUBNET_A" "$SUBNET_B" --security-groups "$ALB_SG" \
             --tags "Key=Name,Value=$ALB_NAME" "Key=Project,Value=$PROJECT" \
             --query 'LoadBalancers[0].LoadBalancerArn' --output text)
  ok "created (provisioning takes 2-4 minutes)"
else
  ok "reusing"
fi

# ---- Listener :80 -> target group ---------------------------------------------------
log "Listener on port $ALB_PORT"
LISTENER_ARN=$(aws elbv2 describe-listeners --load-balancer-arn "$ALB_ARN" \
  --query "Listeners[?Port==\`$ALB_PORT\`].ListenerArn | [0]" --output text 2>/dev/null) || LISTENER_ARN=""
if [[ -z "$LISTENER_ARN" || "$LISTENER_ARN" == "None" ]]; then
  LISTENER_ARN=$(aws elbv2 create-listener --load-balancer-arn "$ALB_ARN" \
                  --protocol HTTP --port "$ALB_PORT" \
                  --default-actions "Type=forward,TargetGroupArn=$TG_ARN" \
                  --query 'Listeners[0].ListenerArn' --output text)
  ok "created: HTTP :$ALB_PORT -> $TG_NAME"
else
  ok "reusing"
fi

# ---- Wait until it is usable ----------------------------------------------------------
log "Waiting for the load balancer to become active"
aws elbv2 wait load-balancer-available --load-balancer-arns "$ALB_ARN"
ok "active"
log "Waiting for both servers to pass health checks"
aws elbv2 wait target-in-service --target-group-arn "$TG_ARN" \
  --targets "Id=$INSTANCE_A" "Id=$INSTANCE_B" \
  || die "Targets not healthy. Is the app running? ./infra/aws/05_servers.sh logs a"
ok "both healthy"

ALB_DNS=$(aws elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" \
           --query 'LoadBalancers[0].DNSName' --output text)
cat > "$ALB_STATE" <<EOF
# Written by 06_load_balancer.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ). Read by verify_alb.sh, distribution_test.sh, commits 7-8.
ALB_ARN=$ALB_ARN
TG_ARN=$TG_ARN
LISTENER_ARN=$LISTENER_ARN
ALB_DNS=$ALB_DNS
EOF

log "Done. Live URL:"
echo
echo "    http://$ALB_DNS/centers"
echo
echo "The DNS name can take a minute or two to resolve the first time. Then:"
echo "    ./infra/aws/verify_alb.sh            # checks everything"
echo "    ./infra/aws/distribution_test.sh     # 200 requests, counts per zone"
