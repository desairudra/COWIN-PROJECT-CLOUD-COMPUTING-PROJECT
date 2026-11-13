#!/usr/bin/env bash
# Checks the network from 01_network.sh is exactly as designed. Prints PASS/FAIL
# per check and exits non-zero if anything is wrong. Its output is good
# evidence for the report: save it to docs/evidence/04-verify-network.txt.
#
#   ./infra/aws/verify_network.sh | tee docs/evidence/04-verify-network.txt
set -uo pipefail
source "$(dirname "$0")/config.sh"
[[ -f "$NETWORK_STATE" ]] || die "No $NETWORK_STATE. Run 01_network.sh first."
source "$NETWORK_STATE"

fails=0
check() {  # check <description> <actual> <expected>
  if [[ "$2" == "$3" ]]; then printf '  PASS  %s\n' "$1"
  else printf '  FAIL  %s (got "%s", want "%s")\n' "$1" "$2" "$3"; fails=$((fails + 1)); fi
}
q() { aws ec2 "$@" --output text 2>/dev/null || echo "error"; }

echo "Network check for $PROJECT in $AWS_REGION ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
check "VPC exists with $VPC_CIDR" \
  "$(q describe-vpcs --vpc-ids "$VPC_ID" --query 'Vpcs[0].CidrBlock')" "$VPC_CIDR"

check "Subnet A is in $AZ_A" \
  "$(q describe-subnets --subnet-ids "$SUBNET_A" --query 'Subnets[0].AvailabilityZone')" "$AZ_A"
check "Subnet B is in $AZ_B" \
  "$(q describe-subnets --subnet-ids "$SUBNET_B" --query 'Subnets[0].AvailabilityZone')" "$AZ_B"
check "Subnets are in different AZs" \
  "$([[ "$AZ_A" != "$AZ_B" ]] && echo yes || echo no)" "yes"
check "Subnet A gives public IPs" \
  "$(q describe-subnets --subnet-ids "$SUBNET_A" --query 'Subnets[0].MapPublicIpOnLaunch')" "True"
check "Subnet B gives public IPs" \
  "$(q describe-subnets --subnet-ids "$SUBNET_B" --query 'Subnets[0].MapPublicIpOnLaunch')" "True"

check "Internet gateway attached to the VPC" \
  "$(q describe-internet-gateways --internet-gateway-ids "$IGW_ID" --query 'InternetGateways[0].Attachments[0].VpcId')" "$VPC_ID"
check "Default route goes to the internet gateway" \
  "$(q describe-route-tables --route-table-ids "$RTB_ID" --query "RouteTables[0].Routes[?DestinationCidrBlock=='0.0.0.0/0'].GatewayId | [0]")" "$IGW_ID"
for sn in "$SUBNET_A" "$SUBNET_B"; do
  check "Route table associated with $sn" \
    "$(q describe-route-tables --route-table-ids "$RTB_ID" --query "length(RouteTables[0].Associations[?SubnetId=='$sn'])")" "1"
done

check "alb SG allows port $ALB_PORT from anywhere" \
  "$(q describe-security-groups --group-ids "$ALB_SG" --query "SecurityGroups[0].IpPermissions[?FromPort==\`$ALB_PORT\`].IpRanges[].CidrIp | [0]")" "0.0.0.0/0"
check "app SG allows port $APP_PORT from the alb SG" \
  "$(q describe-security-groups --group-ids "$APP_SG" --query "SecurityGroups[0].IpPermissions[?FromPort==\`$APP_PORT\`].UserIdGroupPairs[].GroupId | [0]")" "$ALB_SG"
check "app SG does NOT allow port $APP_PORT from anywhere" \
  "$(q describe-security-groups --group-ids "$APP_SG" --query "SecurityGroups[0].IpPermissions[?FromPort==\`$APP_PORT\`].IpRanges[] | [?CidrIp=='0.0.0.0/0'] | length(@)")" "0"

open_ip=$(q describe-security-groups --group-ids "$APP_SG" --query "SecurityGroups[0].IpPermissions[].IpRanges[] | length(@)")
if [[ "$open_ip" != "0" ]]; then
  printf '  NOTE  app SG has %s temporary IP rule(s); remove with ./infra/aws/allow_my_ip.sh remove\n' "$open_ip"
fi

echo
if (( fails == 0 )); then echo "All checks passed."; else echo "$fails check(s) failed."; fi
exit $(( fails > 0 ))
