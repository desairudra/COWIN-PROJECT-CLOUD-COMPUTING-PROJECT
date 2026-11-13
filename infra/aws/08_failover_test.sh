#!/usr/bin/env bash
#  stop Zone A's server while the site is in use and measure what users see.
#
#   ./infra/aws/08_failover_test.sh hard        stop server-a without warning
#   ./infra/aws/08_failover_test.sh graceful    take it out of the load balancer first, then stop it
#
# Probes the live URL 5 times a second for 180 s, stops server-a at 60 s, records the load
# balancer's view of both targets every 2 s, then starts server-a again. Results land in
# docs/evidence/08-failover-<mode>-<time>/ (results.md, timeline.svg, raw CSVs).
#
# Settings: DURATION=180 KILL_AT=60 RATE=5 SKIP_RESTORE=1 PROBE_URL=<url>
set -euo pipefail
source "$(dirname "$0")/config.sh"
for f in network servers alb; do
  [[ -f "$STATE_DIR/$f.env" ]] || die "No $STATE_DIR/$f.env. Run the commit 4-6 scripts first."
  source "$STATE_DIR/$f.env"
done

MODE="${1:-}"
[[ "$MODE" == "hard" || "$MODE" == "graceful" ]] || { echo "usage: $0 hard|graceful"; exit 1; }
DURATION="${DURATION:-180}"; KILL_AT="${KILL_AT:-60}"; RATE="${RATE:-5}"
URL="${PROBE_URL:-http://$ALB_DNS/health}"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RUN_ID="$(date -u +%Y%m%d-%H%M%S)-$MODE"
RUN_DIR="$REPO_ROOT/docs/evidence/08-failover-$RUN_ID"
now() { python3 -c 'import time; print(f"{time.time():.3f}")'; }
since() { python3 -c "print(f'{$(now) - $1:.3f}')"; }
target_state() {
  aws elbv2 describe-target-health --target-group-arn "$TG_ARN" \
    --query "TargetHealthDescriptions[?Target.Id=='$1'].TargetHealth.State | [0]" --output text 2>/dev/null || echo unknown
}

log "Pre-flight"
require_aws
for id in "$INSTANCE_A" "$INSTANCE_B"; do
  st=$(target_state "$id")
  [[ "$st" == "healthy" ]] || die "Target $id is '$st', not healthy. Fix that before testing (verify_alb.sh)."
done
ok "both targets healthy; probing $URL"
mkdir -p "$RUN_DIR"

log "Test running for ${DURATION}s: server-a ($AZ_A) goes down at ${KILL_AT}s ($MODE)"
T0=$(now)
python3 "$SCRIPT_DIR/failover/probe.py" "$URL" "$RUN_DIR/probe.csv" \
  --rate "$RATE" --duration "$DURATION" &
PROBE_PID=$!

(
  echo "t_s,time_utc,instance,zone,state,reason" > "$RUN_DIR/health.csv"
  while kill -0 "$PROBE_PID" 2>/dev/null; do
    t=$(since "$T0"); ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    aws elbv2 describe-target-health --target-group-arn "$TG_ARN" \
      --query 'TargetHealthDescriptions[].[Target.Id,TargetHealth.State,TargetHealth.Reason]' --output text 2>/dev/null \
    | while read -r id state reason; do
        zone=$([[ "$id" == "$INSTANCE_A" ]] && echo "$AZ_A" || echo "$AZ_B")
        echo "$t,$ts,$id,$zone,$state,${reason:-}" >> "$RUN_DIR/health.csv"
      done
    sleep 2
  done
) &
WATCH_PID=$!
trap 'kill $PROBE_PID $WATCH_PID 2>/dev/null || true' EXIT

sleep "$(python3 -c "print(max(0, $T0 + $KILL_AT - $(now)))")"
KILL_AT_S=$(since "$T0")
if [[ "$MODE" == "graceful" ]]; then
  ok "t=${KILL_AT_S}s  removing server-a from the load balancer (draining 10 s)"
  aws elbv2 deregister-targets --target-group-arn "$TG_ARN" --targets "Id=$INSTANCE_A"
  aws elbv2 wait target-deregistered --target-group-arn "$TG_ARN" --targets "Id=$INSTANCE_A"
fi
STOP_AT_S=$(since "$T0")
aws ec2 stop-instances --instance-ids "$INSTANCE_A" >/dev/null
ok "t=${STOP_AT_S}s  server-a stop requested"
[[ -n "${ON_KILL:-}" ]] && bash -c "$ON_KILL"

while kill -0 "$PROBE_PID" 2>/dev/null; do
  printf '\r    t=%5.0fs  zone A target: %-12s' "$(since "$T0")" "$(target_state "$INSTANCE_A")"
  sleep 5
done
echo
wait "$PROBE_PID" || true
kill "$WATCH_PID" 2>/dev/null || true
trap - EXIT
ok "probe finished"

cat > "$RUN_DIR/run.env" <<EOF
RUN_ID=$RUN_ID
MODE=$MODE
URL=$URL
RATE=$RATE
DURATION=$DURATION
KILL_AT_S=$KILL_AT_S
STOP_AT_S=$STOP_AT_S
RESTORE_AT_S=
AZ_A=$AZ_A
AZ_B=$AZ_B
INSTANCE_A=$INSTANCE_A
INSTANCE_B=$INSTANCE_B
EOF

log "Results"
python3 "$SCRIPT_DIR/failover/analyze.py" "$RUN_DIR"

if [[ -z "${SKIP_RESTORE:-}" ]]; then
  log "Restoring server-a"
  aws ec2 wait instance-stopped --instance-ids "$INSTANCE_A"
  aws ec2 start-instances --instance-ids "$INSTANCE_A" >/dev/null
  aws ec2 wait instance-running --instance-ids "$INSTANCE_A"
  aws elbv2 register-targets --target-group-arn "$TG_ARN" --targets "Id=$INSTANCE_A"
  new_ip=$(aws ec2 describe-instances --instance-ids "$INSTANCE_A" \
            --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
  sed -i.bak "s/^IP_A=.*/IP_A=$new_ip/" "$STATE_DIR/servers.env" && rm -f "$STATE_DIR/servers.env.bak"
  aws elbv2 wait target-in-service --target-group-arn "$TG_ARN" --targets "Id=$INSTANCE_A" \
    && ok "server-a is back and healthy" \
    || warn "server-a not healthy yet; check ./infra/aws/verify_alb.sh in a minute"
fi

log "Saved to ${RUN_DIR#$REPO_ROOT/}"
