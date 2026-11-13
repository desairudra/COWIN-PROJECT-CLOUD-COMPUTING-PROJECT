#!/usr/bin/env bash
#  the load balancer, listener, health-check settings, both
# targets healthy, and the live URL answering. PASS/FAIL per check.
#
#   ./infra/aws/verify_alb.sh | tee docs/evidence/06-verify-alb.txt
set -uo pipefail
source "$(dirname "$0")/config.sh"
for f in network servers alb; do
  [[ -f "$STATE_DIR/$f.env" ]] || die "No $STATE_DIR/$f.env. Run the earlier scripts first."
  source "$STATE_DIR/$f.env"
done

fails=0
check() {
  if [[ "$2" == "$3" ]]; then printf '  PASS  %s\n' "$1"
  else printf '  FAIL  %s (got "%s", want "%s")\n' "$1" "$2" "$3"; fails=$((fails + 1)); fi
}
lb() { aws elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" --query "LoadBalancers[0].$1" --output text 2>/dev/null || echo error; }
tg() { aws elbv2 describe-target-groups --target-group-arns "$TG_ARN" --query "TargetGroups[0].$1" --output text 2>/dev/null || echo error; }
health() { aws elbv2 describe-target-health --target-group-arn "$TG_ARN" \
             --query "TargetHealthDescriptions[?Target.Id=='$1'].TargetHealth.State | [0]" --output text 2>/dev/null || echo error; }

echo "Load balancer check for $PROJECT in $AWS_REGION ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
check "load balancer is active"                  "$(lb State.Code)" "active"
check "it is internet-facing"                    "$(lb Scheme)" "internet-facing"
check "it is an Application Load Balancer"       "$(lb Type)" "application"
check "it sits in both subnets"                  "$(lb 'AvailabilityZones[].SubnetId | sort(@) | join(`,`, @)')" \
                                                 "$(printf '%s\n' "$SUBNET_A" "$SUBNET_B" | sort | paste -sd, -)"
check "it spans $AZ_A and $AZ_B"                 "$(lb 'AvailabilityZones[].ZoneName | sort(@) | join(`,`, @)')" "$AZ_A,$AZ_B"
check "it uses the alb security group"           "$(lb 'SecurityGroups | join(`,`, @)')" "$ALB_SG"
check "listener: HTTP :$ALB_PORT forwards to the target group" \
  "$(aws elbv2 describe-listeners --load-balancer-arn "$ALB_ARN" \
      --query "Listeners[?Port==\`$ALB_PORT\`].DefaultActions[0].TargetGroupArn | [0]" --output text 2>/dev/null)" "$TG_ARN"

check "targets on port $APP_PORT"                "$(tg Port)" "$APP_PORT"
check "health check path /health"                "$(tg HealthCheckPath)" "/health"
check "health check every 5 s"                   "$(tg HealthCheckIntervalSeconds)" "5"
check "unhealthy after 2 failed checks"          "$(tg UnhealthyThresholdCount)" "2"
check "healthy after 2 passed checks"            "$(tg HealthyThresholdCount)" "2"
check "deregistration delay 10 s" \
  "$(aws elbv2 describe-target-group-attributes --target-group-arn "$TG_ARN" \
      --query "Attributes[?Key=='deregistration_delay.timeout_seconds'].Value | [0]" --output text 2>/dev/null)" "10"
check "exactly 2 targets registered" \
  "$(aws elbv2 describe-target-health --target-group-arn "$TG_ARN" --query 'length(TargetHealthDescriptions)' --output text 2>/dev/null)" "2"
check "server-a ($AZ_A) is healthy"              "$(health "$INSTANCE_A")" "healthy"
check "server-b ($AZ_B) is healthy"              "$(health "$INSTANCE_B")" "healthy"

if [[ -z "${SKIP_HTTP:-}" ]]; then
  # A new ALB's DNS name can take a minute to resolve: retry for up to 2 minutes.
  code=000
  for i in $(seq 1 24); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://$ALB_DNS/health" 2>/dev/null); code="${code:-000}"
    [[ "$code" == "200" ]] && break
    sleep 5
  done
  check "live URL http://$ALB_DNS/health answers" "$code" "200"
  direct=$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "http://$IP_A:$APP_PORT/health" 2>/dev/null); direct="${direct:-000}"
  if [[ "$direct" == "000" ]]; then printf '  PASS  servers are NOT reachable directly (only through the load balancer)\n'
  else printf '  NOTE  server-a answered directly (HTTP %s): a temporary allow_my_ip rule is still open; run allow_my_ip.sh remove\n' "$direct"; fi
fi

echo
echo "Live URL: http://$ALB_DNS/centers"
if (( fails == 0 )); then echo "All checks passed."; else echo "$fails check(s) failed."; fi
exit $(( fails > 0 ))