#!/usr/bin/env bash
# Stop a server and watch the Auto Scaling group replace it, while a probe keeps using the site.
#   ./infra/cfn/replace_test.sh          stops the server in zone a at 60 s, runs 8 min
# Env: DURATION=480  STOP_AT=60  RATE=5  ZONE=a
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/../aws/config.sh"
ROOT="$(cd "$HERE/../.." && pwd)"
DURATION="${DURATION:-480}"
STOP_AT="${STOP_AT:-60}"
RATE="${RATE:-5}"
ZONE="$AWS_REGION${ZONE:-a}"
[[ -f "$STATE_DIR/cfn.env" ]] || die "no stack state: run ./infra/cfn/stack.sh up first"
. "$STATE_DIR/cfn.env"
require_aws

RUN="$ROOT/docs/evidence/15-replace-$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "$RUN"
URL="http://$ALB_DNS/centers?city=Mumbai"

victim=$(aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$ASG" \
  --query "AutoScalingGroups[0].Instances[?AvailabilityZone=='$ZONE' && LifecycleState=='InService'].InstanceId | [0]" --output text)
[[ -n "$victim" && "$victim" != "None" ]] || die "no in-service server in $ZONE"
before=$(aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$ASG" \
  --query "AutoScalingGroups[0].Instances[].InstanceId" --output text)

cat > "$RUN/run.env" <<ENV
EXPERIMENT=E15
TITLE=Auto Scaling group replaces a stopped server
GROUP=node
GROUP_LABEL=zone
UNIT=servers
RATE=$RATE
DURATION=$DURATION
PODS=$(echo "$before" | wc -w)
URL=$URL
VICTIM=$victim
ASG=$ASG
ENV
printf 'node,zone\n%s,%s\n%s,%s\n' "${AWS_REGION}a" "${AWS_REGION}a" "${AWS_REGION}b" "${AWS_REGION}b" > "$RUN/nodes.csv"
echo "t_s,event" > "$RUN/events.csv"

T0=$(date +%s.%N)
now() { awk -v a="$T0" -v b="$(date +%s.%N)" 'BEGIN{printf "%.2f", b-a}'; }
event() { echo "$(now),$1" >> "$RUN/events.csv"; echo "    t=$(now)s  $1"; }

# Same columns as the Kubernetes runs, so experiments/k8s/analyze.py can read it:
# a server is "ready" when the load balancer says it is healthy.
sample() {
  local seen_unhealthy="" seen_new="" seen_healthy=""
  echo "t_s,pod,node,phase,terminating,ready,release" > "$RUN/pods.csv"
  while :; do
    local t asg tg
    t=$(now)
    asg=$(aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$ASG" \
      --query 'AutoScalingGroups[0].Instances[].[InstanceId,AvailabilityZone,LifecycleState,HealthStatus]' --output text 2>/dev/null || true)
    tg=$(aws elbv2 describe-target-health --target-group-arn "$TG_ARN" \
      --query 'TargetHealthDescriptions[].[Target.Id,TargetHealth.State]' --output text 2>/dev/null || true)
    while read -r id az state health; do
      [[ -n "$id" ]] || continue
      tgs=$(awk -v i="$id" '$1==i{print $2}' <<< "$tg")
      term=0; [[ "$state" == Terminating* ]] && term=1
      ready=False; [[ "$tgs" == healthy ]] && ready=True
      echo "$t,$id,$az,$state,$term,$ready,${tgs:-none}" >> "$RUN/pods.csv"
      if [[ "$id" == "$victim" && "$health" == Unhealthy && -z "$seen_unhealthy" ]]; then
        seen_unhealthy=1; event "group marks $id unhealthy"
      fi
      if ! grep -qw "$id" <<< "$before"; then
        [[ -z "$seen_new" ]] && { seen_new=1; event "replacement $id launched in $az"; }
        [[ "$ready" == True && -z "$seen_healthy" ]] && { seen_healthy=1; event "replacement healthy"; }
      fi
    done <<< "$asg"
    sleep 5
  done
}

log "Probing $URL at $RATE/s for $DURATION s; stopping $victim ($ZONE) at $STOP_AT s"
python3 "$ROOT/infra/aws/failover/probe.py" "$URL" "$RUN/probe.csv" --rate "$RATE" --duration "$DURATION" --timeout 2 &
probe=$!
sample & sampler=$!
trap 'kill $probe $sampler 2>/dev/null || true' EXIT

while (( $(awk -v n="$(now)" -v t="$STOP_AT" 'BEGIN{print (n<t)}') )); do sleep 0.5; done
aws ec2 stop-instances --instance-ids "$victim" >/dev/null
event "stopped $victim"

wait "$probe"
kill "$sampler" 2>/dev/null || true
trap - EXIT
aws autoscaling describe-scaling-activities --auto-scaling-group-name "$ASG" --max-items 5 \
  --query 'Activities[].[StartTime,StatusCode,Description]' --output text > "$RUN/activities.txt" || true

python3 "$ROOT/experiments/k8s/analyze.py" "$RUN"
echo "Group now:"
aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$ASG" \
  --query 'AutoScalingGroups[0].Instances[].[InstanceId,AvailabilityZone,LifecycleState,HealthStatus]' --output table
