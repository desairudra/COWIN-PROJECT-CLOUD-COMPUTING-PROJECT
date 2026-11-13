#!/usr/bin/env bash
# Commit 4 (Step 3 prerequisite): the network for the two-AZ deployment.
#
#   VPC 10.0.0.0/16
#   ├── public subnet A 10.0.1.0/24 in ap-south-1a  ┐ both routed to the internet
#   ├── public subnet B 10.0.2.0/24 in ap-south-1b  ┘ through one internet gateway
#   ├── security group "alb": port 80 from anywhere          (the load balancer, commit 6)
#   └── security group "app": port 8000 only from "alb"      (the EC2 servers, commit 5)
#
# Safe to run again: anything that already exists (found by its Name tag) is
# reused, not duplicated. IDs are saved to infra/aws/.state/network.env for
# commits 5 and 6.
#
#   ./infra/aws/01_network.sh
set -euo pipefail
source "$(dirname "$0")/config.sh"

log "Checking AWS access"
require_aws

log "Checking the two Availability Zones exist"
for az in "$AZ_A" "$AZ_B"; do
  state=$(aws ec2 describe-availability-zones --zone-names "$az" \
          --query 'AvailabilityZones[0].State' --output text 2>/dev/null) || state=""
  [[ "$state" == "available" ]] || die "Availability Zone $az is not available in $AWS_REGION"
  ok "$az available"
done

# ---- VPC ---------------------------------------------------------------------------
log "VPC"
VPC_ID=$(find_by_name describe-vpcs 'Vpcs[0].VpcId' vpc)
if [[ -z "$VPC_ID" ]]; then
  VPC_ID=$(aws ec2 create-vpc --cidr-block "$VPC_CIDR" \
           --tag-specifications "$(tags vpc vpc)" --query Vpc.VpcId --output text)
  aws ec2 wait vpc-available --vpc-ids "$VPC_ID"
  ok "created $VPC_ID ($VPC_CIDR)"
else
  ok "reusing $VPC_ID"
fi
# DNS names for instances; the ALB itself does not need these, but they make debugging easier.
aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-support '{"Value":true}'
aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-hostnames '{"Value":true}'

# ---- Internet gateway --------------------------------------------------------------
log "Internet gateway"
IGW_ID=$(find_by_name describe-internet-gateways 'InternetGateways[0].InternetGatewayId' igw)
if [[ -z "$IGW_ID" ]]; then
  IGW_ID=$(aws ec2 create-internet-gateway --tag-specifications "$(tags internet-gateway igw)" \
           --query InternetGateway.InternetGatewayId --output text)
  ok "created $IGW_ID"
else
  ok "reusing $IGW_ID"
fi
attached=$(aws ec2 describe-internet-gateways --internet-gateway-ids "$IGW_ID" \
           --query 'InternetGateways[0].Attachments[0].VpcId' --output text)
if [[ "$attached" != "$VPC_ID" ]]; then
  aws ec2 attach-internet-gateway --internet-gateway-id "$IGW_ID" --vpc-id "$VPC_ID"
  ok "attached to $VPC_ID"
else
  ok "already attached"
fi

# ---- Subnets, one per AZ -------------------------------------------------------------
make_subnet() {  # make_subnet <suffix> <cidr> <az>  -> prints subnet id
  local id
  id=$(find_by_name describe-subnets 'Subnets[0].SubnetId' "$1")
  if [[ -z "$id" ]]; then
    id=$(aws ec2 create-subnet --vpc-id "$VPC_ID" --cidr-block "$2" --availability-zone "$3" \
         --tag-specifications "$(tags subnet "$1")" --query Subnet.SubnetId --output text)
  fi
  # Servers get a public IP so they can download packages at boot (commit 5).
  # Inbound traffic is still blocked by the "app" security group.
  aws ec2 modify-subnet-attribute --subnet-id "$id" --map-public-ip-on-launch
  echo "$id"
}
log "Subnets"
SUBNET_A=$(make_subnet subnet-a "$SUBNET_A_CIDR" "$AZ_A"); ok "$SUBNET_A  $SUBNET_A_CIDR in $AZ_A"
SUBNET_B=$(make_subnet subnet-b "$SUBNET_B_CIDR" "$AZ_B"); ok "$SUBNET_B  $SUBNET_B_CIDR in $AZ_B"

# ---- Route table: 0.0.0.0/0 -> internet gateway ----------------------------------------
log "Route table"
RTB_ID=$(find_by_name describe-route-tables 'RouteTables[0].RouteTableId' public-rt)
if [[ -z "$RTB_ID" ]]; then
  RTB_ID=$(aws ec2 create-route-table --vpc-id "$VPC_ID" \
           --tag-specifications "$(tags route-table public-rt)" --query RouteTable.RouteTableId --output text)
  ok "created $RTB_ID"
else
  ok "reusing $RTB_ID"
fi
has_route=$(aws ec2 describe-route-tables --route-table-ids "$RTB_ID" \
  --query "RouteTables[0].Routes[?DestinationCidrBlock=='0.0.0.0/0'].GatewayId" --output text)
if [[ "$has_route" != "$IGW_ID" ]]; then
  aws ec2 create-route --route-table-id "$RTB_ID" --destination-cidr-block 0.0.0.0/0 \
      --gateway-id "$IGW_ID" >/dev/null
fi
ok "0.0.0.0/0 -> $IGW_ID"
for sn in "$SUBNET_A" "$SUBNET_B"; do
  assoc=$(aws ec2 describe-route-tables --route-table-ids "$RTB_ID" \
    --query "RouteTables[0].Associations[?SubnetId=='$sn'].RouteTableAssociationId" --output text)
  if [[ -z "$assoc" || "$assoc" == "None" ]]; then
    aws ec2 associate-route-table --route-table-id "$RTB_ID" --subnet-id "$sn" >/dev/null
  fi
  ok "associated with $sn"
done

# ---- Security groups ---------------------------------------------------------------------
make_sg() {  # make_sg <suffix> <description>  -> prints group id
  local id
  id=$(aws ec2 describe-security-groups \
         --filters "Name=vpc-id,Values=$VPC_ID" "Name=group-name,Values=$PROJECT-$1" \
         --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null) || id=""
  if [[ -z "$id" || "$id" == "None" ]]; then
    id=$(aws ec2 create-security-group --vpc-id "$VPC_ID" --group-name "$PROJECT-$1" \
         --description "$2" --tag-specifications "$(tags security-group "$1")" \
         --query GroupId --output text)
  fi
  echo "$id"
}
# Adds an inbound rule unless it is already there ("Duplicate" errors are ignored).
allow() {  # allow <group-id> <port> (cidr <x.x.x.x/n> | sg <group-id>) <description>
  local perm
  if [[ "$3" == "cidr" ]]; then
    perm="IpProtocol=tcp,FromPort=$2,ToPort=$2,IpRanges=[{CidrIp=$4,Description=\"$5\"}]"
  else
    perm="IpProtocol=tcp,FromPort=$2,ToPort=$2,UserIdGroupPairs=[{GroupId=$4,Description=\"$5\"}]"
  fi
  aws ec2 authorize-security-group-ingress --group-id "$1" --ip-permissions "$perm" >/dev/null 2>&1 \
    || true
}
log "Security groups"
ALB_SG=$(make_sg alb "Load balancer: HTTP from the internet")
allow "$ALB_SG" "$ALB_PORT" cidr 0.0.0.0/0 "HTTP from anywhere"
ok "$ALB_SG  alb: port $ALB_PORT from 0.0.0.0/0"

APP_SG=$(make_sg app "Lookup servers: port $APP_PORT from the load balancer only")
allow "$APP_SG" "$APP_PORT" sg "$ALB_SG" "Only the load balancer may reach the app"
ok "$APP_SG  app: port $APP_PORT from $ALB_SG only"

# ---- Save IDs for commits 5 and 6 ---------------------------------------------------------
mkdir -p "$STATE_DIR"
cat > "$NETWORK_STATE" <<EOF
# Written by 01_network.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ). Read by the commit 5 and 6 scripts.
AWS_REGION=$AWS_REGION
VPC_ID=$VPC_ID
IGW_ID=$IGW_ID
RTB_ID=$RTB_ID
SUBNET_A=$SUBNET_A
SUBNET_B=$SUBNET_B
AZ_A=$AZ_A
AZ_B=$AZ_B
ALB_SG=$ALB_SG
APP_SG=$APP_SG
EOF
log "Done. IDs saved to ${NETWORK_STATE#$PWD/}"
cat "$NETWORK_STATE" | grep -v '^#' | sed 's/^/    /'
echo
echo "Next: ./infra/aws/verify_network.sh   (and take the console screenshots listed in infra/aws/README.md)"
