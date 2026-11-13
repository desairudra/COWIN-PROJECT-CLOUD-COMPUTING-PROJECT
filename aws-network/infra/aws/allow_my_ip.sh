#!/usr/bin/env bash
# Temporarily lets YOUR computer reach the EC2 servers directly, for checking
# each server in commit 5 before the load balancer exists (port 8000), and
# optionally SSH (port 22). Remove it again afterwards: in the final setup only
# the load balancer can reach the servers.
#
#   ./infra/aws/allow_my_ip.sh add        # port 8000 from your current public IP
#   ./infra/aws/allow_my_ip.sh add-ssh    # port 22 as well
#   ./infra/aws/allow_my_ip.sh remove     # remove every rule this script added
set -euo pipefail
source "$(dirname "$0")/config.sh"
[[ -f "$NETWORK_STATE" ]] || die "No $NETWORK_STATE. Run 01_network.sh first."
source "$NETWORK_STATE"

action="${1:-}"
DESC="temporary: allow_my_ip.sh"

my_ip() {
  local ip
  ip=$(curl -s --max-time 5 https://checkip.amazonaws.com | tr -d '[:space:]') || true
  [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "Could not find your public IP. Set MY_IP=x.x.x.x and retry."
  echo "$ip"
}

add_rule() {  # add_rule <port> <ip>
  aws ec2 authorize-security-group-ingress --group-id "$APP_SG" --ip-permissions \
    "IpProtocol=tcp,FromPort=$1,ToPort=$1,IpRanges=[{CidrIp=$2/32,Description=\"$DESC\"}]" >/dev/null 2>&1 \
    && ok "port $1 open to $2/32 on $APP_SG" || ok "port $1 already open to $2/32"
}

case "$action" in
  add|add-ssh)
    ip="${MY_IP:-$(my_ip)}"
    add_rule "$APP_PORT" "$ip"
    [[ "$action" == "add-ssh" ]] && add_rule 22 "$ip"
    warn "Remember: ./infra/aws/allow_my_ip.sh remove   when you are done"
    ;;
  remove)
    # Every IP-range rule on the app group is temporary: the real access path is the alb SG.
    perms=$(aws ec2 describe-security-groups --group-ids "$APP_SG" \
      --query 'SecurityGroups[0].IpPermissions[?length(IpRanges) > `0`].{IpProtocol:IpProtocol,FromPort:FromPort,ToPort:ToPort,IpRanges:IpRanges}' \
      --output json)
    if [[ "$perms" == "[]" ]]; then ok "nothing to remove"; exit 0; fi
    aws ec2 revoke-security-group-ingress --group-id "$APP_SG" --ip-permissions "$perms" >/dev/null
    ok "removed temporary IP rules from $APP_SG"
    ;;
  *)
    echo "usage: $0 add | add-ssh | remove"; exit 1 ;;
esac
