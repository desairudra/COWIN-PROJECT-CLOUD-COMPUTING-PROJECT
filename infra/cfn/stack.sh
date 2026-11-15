#!/usr/bin/env bash
# The whole AWS setup as one CloudFormation stack (commit 15).
#   ./infra/cfn/stack.sh up       build or update it (about 5 min), prints the live URL
#   ./infra/cfn/stack.sh status   servers in the Auto Scaling group and their health
#   ./infra/cfn/stack.sh down     delete everything (about 5 min)
#   ./infra/cfn/stack.sh check    validate the template with AWS, change nothing
# Env: STACK=cowin-it457-cfn  MIN_SIZE=2  MAX_SIZE=4  INSTANCE_TYPE=t3.micro  YES=1 (down without asking)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/../aws/config.sh"
STACK="${STACK:-cowin-it457-cfn}"
TEMPLATE="$HERE/cowin-stack.yaml"
RENDERED="$STATE_DIR/cowin-stack.rendered.yaml"
CFN_STATE="$STATE_DIR/cfn.env"
mkdir -p "$STATE_DIR"

render() {
  "$HERE/../aws/05_servers.sh" render > "$STATE_DIR/user-data.sh"
  python3 - "$TEMPLATE" "$STATE_DIR/user-data.sh" "$RENDERED" <<'PY'
import sys
template, user_data, out = sys.argv[1:]
lines = open(template).read().split("\n")
script = open(user_data).read().rstrip("\n").split("\n")
i = next(n for n, l in enumerate(lines) if l.strip() == "__USER_DATA__")
indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
lines[i:i + 1] = [(indent + s) if s else "" for s in script]
open(out, "w").write("\n".join(lines))
PY
  local size; size=$(wc -c < "$RENDERED")
  (( size <= 51200 )) || die "template is $size bytes; CloudFormation takes 51,200 without an S3 bucket"
  ok "template with the app inside: $size bytes"
}

NAME=cowin-cfn

save_state() {
  local alb tg
  alb=$(aws elbv2 describe-load-balancers --names "$NAME-alb" --query 'LoadBalancers[0].[LoadBalancerArn,DNSName]' --output text)
  tg=$(aws elbv2 describe-target-groups --names "$NAME-tg" --query 'TargetGroups[0].TargetGroupArn' --output text)
  {
    echo "STACK=$STACK"
    echo "ALB_DNS=$(cut -f2 <<< "$alb")"
    echo "ALB_DIM=$(cut -f1 <<< "$alb" | sed 's#.*:loadbalancer/##')"
    echo "TG_ARN=$tg"
    echo "TG_DIM=${tg##*:}"
    echo "ASG=$NAME-asg"
  } > "$CFN_STATE"
}

stack_status() {
  aws cloudformation describe-stacks --stack-name "$STACK" --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo NONE
}

why_failed() {
  aws cloudformation describe-stack-events --stack-name "$STACK" \
    --query "StackEvents[?contains(ResourceStatus,'FAILED')].[LogicalResourceId,ResourceStatusReason] | [0:5]" --output text >&2 || true
}

healthy_count() {
  aws elbv2 describe-target-health --target-group-arn "$1" \
    --query "length(TargetHealthDescriptions[?TargetHealth.State=='healthy'])" --output text
}

servers() {
  aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$1" \
    --query 'AutoScalingGroups[0].Instances[].[InstanceId,AvailabilityZone,LifecycleState,HealthStatus]' --output table
}

case "${1:-}" in
  check)
    require_aws
    render
    aws cloudformation validate-template --template-body "file://$RENDERED" >/dev/null
    ok "AWS accepts the template"
    ;;
  up)
    require_aws
    log "Template"
    render
    if [[ "$(stack_status)" == ROLLBACK_COMPLETE ]]; then
      warn "an earlier attempt failed and rolled back; deleting it first"
      aws cloudformation delete-stack --stack-name "$STACK"
      aws cloudformation wait stack-delete-complete --stack-name "$STACK"
    fi
    log "Stack $STACK (5 min the first time; CloudFormation prints its progress only at the end)"
    aws cloudformation deploy --stack-name "$STACK" --template-file "$RENDERED" \
      --parameter-overrides MinSize="${MIN_SIZE:-2}" MaxSize="${MAX_SIZE:-4}" InstanceType="${INSTANCE_TYPE:-t3.micro}" \
      --tags Project=cowin-cfn --no-fail-on-empty-changeset || { why_failed; die "stack failed; reasons above"; }
    save_state
    . "$CFN_STATE"
    ok "outputs saved to infra/aws/.state/cfn.env"
    log "Waiting for ${MIN_SIZE:-2} healthy servers (they install the app on boot: 2-4 min)"
    for _ in $(seq 72); do
      n=$(healthy_count "$TG_ARN")
      [[ "$n" -ge "${MIN_SIZE:-2}" ]] && break
      sleep 5
    done
    servers "$ASG"
    [[ "$(healthy_count "$TG_ARN")" -ge "${MIN_SIZE:-2}" ]] || die "servers not healthy yet; run '$0 status' in a minute"
    log "20 requests through the load balancer"
    for _ in $(seq 20); do
      curl -s --max-time 5 "http://$ALB_DNS/health" | python3 -c "import sys,json; d=json.load(sys.stdin)['served_by']; print(d['availability_zone'], d['instance_id'])" 2>/dev/null || echo "no answer"
    done | sort | uniq -c
    echo
    echo "Live URL: http://$ALB_DNS/centers"
    echo "Replacement test: ./infra/cfn/replace_test.sh     Delete: $0 down"
    ;;
  status)
    require_aws
    aws cloudformation describe-stacks --stack-name "$STACK" --query 'Stacks[0].[StackName,StackStatus]' --output table
    [[ -f "$CFN_STATE" ]] || save_state
    . "$CFN_STATE"
    servers "$ASG"
    aws elbv2 describe-target-health --target-group-arn "$TG_ARN" \
      --query 'TargetHealthDescriptions[].[Target.Id,TargetHealth.State,TargetHealth.Reason]' --output table
    echo "Live URL: http://$ALB_DNS/centers"
    ;;
  down)
    require_aws
    if [[ "${YES:-}" != 1 ]]; then
      read -r -p "Delete stack $STACK and everything in it? [y/N] " a
      [[ "$a" =~ ^[Yy]$ ]] || exit 0
    fi
    aws cloudformation delete-stack --stack-name "$STACK"
    log "Deleting (about 5 min)"
    aws cloudformation wait stack-delete-complete --stack-name "$STACK" || { why_failed; die "delete failed; run '$0 down' again"; }
    rm -f "$CFN_STATE" "$RENDERED" "$STATE_DIR/user-data.sh"
    ok "stack $STACK deleted"
    ;;
  *) sed -n '2,7p' "$0"; exit 1 ;;
esac
