# Shared by run_e1.sh, run_e2.sh, run_e3.sh. Source it, don't run it.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
NS=cowin
PROBE="$ROOT/infra/aws/failover/probe.py"

die() { echo "error: $*" >&2; exit 1; }

need_cluster() {
  for t in kubectl curl python3; do command -v "$t" >/dev/null || die "missing $t"; done
  kubectl config use-context kind-cowin >/dev/null 2>&1 || die "no kind-cowin cluster: run ./deploy/k8s/up.sh"
  curl -sf -o /dev/null http://localhost:8080/health || die "nothing on localhost:8080: run ./deploy/k8s/up.sh"
}

new_run() {
  RUN="$ROOT/docs/evidence/14-$1-$(date -u +%Y%m%d-%H%M%S)"
  mkdir -p "$RUN"
  kubectl get nodes -L topology.kubernetes.io/zone --no-headers \
    | awk 'BEGIN{print "node,zone"} {print $1","($6==""?"-":$6)}' > "$RUN/nodes.csv"
  echo "t_s,event" > "$RUN/events.csv"
}

start_clock() { T0=$(date +%s.%N); }
now() { awk -v a="$T0" -v b="$(date +%s.%N)" 'BEGIN{printf "%.2f", b-a}'; }
event() { echo "$(now),$1" >> "$RUN/events.csv"; echo "    t=$(now)s  $1"; }

set_min_replicas() {
  kubectl -n "$NS" patch hpa lookup --type=merge -p "{\"spec\":{\"minReplicas\":$1}}" >/dev/null
}

wait_ready() {
  local want=$1
  for _ in $(seq 90); do
    local n
    n=$(kubectl -n "$NS" get deploy lookup -o jsonpath='{.status.readyReplicas}')
    [[ "${n:-0}" -ge "$want" ]] && return 0
    sleep 2
  done
  die "lookup never reached $want ready pods"
}

# One row per lookup pod per second: t_s,pod,node,phase,terminating,ready,release
sample_pods() {
  echo "t_s,pod,node,phase,terminating,ready,release" > "$RUN/pods.csv"
  while :; do
    local t
    t=$(now)
    kubectl -n "$NS" get pods -l app=lookup -o jsonpath='{range .items[*]}{.metadata.name},{.spec.nodeName},{.status.phase},{.metadata.deletionTimestamp},{.status.conditions[?(@.type=="Ready")].status},{.spec.containers[0].env[?(@.name=="APP_RELEASE")].value}{"\n"}{end}' 2>/dev/null \
      | awk -v t="$t" 'NF{split($0,f,","); print t","f[1]","f[2]","f[3]","(f[4]==""?0:1)","f[5]","f[6]}' >> "$RUN/pods.csv"
    sleep 1
  done
}

stop_bg() { for p in "$@"; do kill "$p" 2>/dev/null || true; done; wait 2>/dev/null || true; }
