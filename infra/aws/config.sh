# Shared settings for the AWS scripts (commits 4, 5, 6). Sourced, not run.
# Override any value by exporting it before running a script, e.g.
#   AWS_REGION=ap-south-1 PROJECT=cowin-team7 ./infra/aws/01_network.sh

export AWS_REGION="${AWS_REGION:-ap-south-1}"          # Mumbai
export AWS_DEFAULT_REGION="$AWS_REGION"
PROJECT="${PROJECT:-cowin-it457}"                      # prefix for every Name tag

VPC_CIDR="${VPC_CIDR:-10.0.0.0/16}"
AZ_A="${AZ_A:-${AWS_REGION}a}"                         # Zone A: shut down in the failover test
AZ_B="${AZ_B:-${AWS_REGION}b}"                         # Zone B: keeps serving
SUBNET_A_CIDR="${SUBNET_A_CIDR:-10.0.1.0/24}"
SUBNET_B_CIDR="${SUBNET_B_CIDR:-10.0.2.0/24}"

ALB_PORT=80        # what users hit
APP_PORT=8000      # where the lookup service listens (commit 3)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="$SCRIPT_DIR/.state"
NETWORK_STATE="$STATE_DIR/network.env"                 # IDs written by 01, read by 05/06

# ---- helpers ------------------------------------------------------------------

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m  %s\n' "$*"; }
warn() { printf '    \033[33m!!\033[0m  %s\n' "$*"; }
die()  { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# Tags every resource gets, so they can be found and torn down by tag.
tags() {  # tags <resource-type> <name-suffix>
  echo "ResourceType=$1,Tags=[{Key=Name,Value=$PROJECT-$2},{Key=Project,Value=$PROJECT}]"
}

# Prints the ID of the resource whose Name tag is $PROJECT-<suffix>, or nothing.
find_by_name() {  # find_by_name <describe-command> <query-path> <suffix>
  local out
  out=$(aws ec2 "$1" --filters "Name=tag:Name,Values=$PROJECT-$3" \
        --query "$2" --output text 2>/dev/null) || true
  [[ "$out" == "None" ]] && out=""
  echo "$out" | awk '{print $1}'
}

require_aws() {
  command -v aws >/dev/null || die "AWS CLI not found. Install it, or use AWS CloudShell (it is preinstalled there)."
  local who
  who=$(aws sts get-caller-identity --query Arn --output text 2>/dev/null) \
    || die "AWS CLI is not logged in. Run 'aws configure' (or use CloudShell)."
  ok "logged in as $who"
  ok "region $AWS_REGION"
}
