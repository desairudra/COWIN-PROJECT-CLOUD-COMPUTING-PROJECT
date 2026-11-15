#!/usr/bin/env bash
# Local Kubernetes (kind) with the lookup service, nginx in front and the HPA.
#   ./deploy/k8s/up.sh        then http://localhost:8080/centers (door in zone a), :8081 (zone b)
set -euo pipefail
cd "$(dirname "$0")/../.."
CLUSTER=cowin
METRICS_URL=https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml

for t in docker kind kubectl; do
  command -v "$t" >/dev/null || { echo "missing: $t (see deploy/README.md)" >&2; exit 1; }
done
docker info >/dev/null 2>&1 || { echo "Docker is not running: sudo systemctl start docker" >&2; exit 1; }

echo "==> Cluster"
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo "    reusing kind-$CLUSTER"
else
  kind create cluster --config deploy/k8s/kind-cluster.yaml
fi
kubectl config use-context "kind-$CLUSTER" >/dev/null
kubectl wait --for=condition=Ready nodes --all --timeout=180s >/dev/null
kubectl get nodes -L topology.kubernetes.io/zone

echo "==> Image"
docker build -q -t cowin/lookup:v1 lookup
kind load docker-image cowin/lookup:v1 --name "$CLUSTER"

echo "==> metrics-server (CPU numbers for the autoscaler)"
kubectl apply -f "$METRICS_URL" >/dev/null
if ! kubectl -n kube-system get deploy metrics-server -o jsonpath='{.spec.template.spec.containers[0].args}' | grep -q kubelet-insecure-tls; then
  kubectl -n kube-system patch deploy metrics-server --type=json \
    -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
fi

echo "==> Lookup service, autoscaler, nginx"
kubectl apply -f deploy/k8s/lookup.yaml -f deploy/k8s/nginx.yaml
for d in lookup nginx; do kubectl -n cowin rollout status "deploy/$d" --timeout=180s; done
kubectl -n kube-system rollout status deploy/metrics-server --timeout=180s

echo "==> Waiting for both doors"
for port in 8080 8081; do
  for _ in $(seq 60); do curl -sf --max-time 3 -o /dev/null "http://localhost:$port/health" && break; sleep 2; done
  curl -sf --max-time 3 -o /dev/null "http://localhost:$port/health" || { echo "nothing on :$port" >&2; exit 1; }
done

echo "==> 20 requests through the zone a door"
for _ in $(seq 20); do
  curl -s --max-time 3 -D - -o /dev/null http://localhost:8080/health | tr -d '\r' | awk -F': ' 'tolower($1)=="x-instance-id"{p=$2} tolower($1)=="x-availability-zone"{n=$2} END{print p" on "n}'
done | sort | uniq -c

kubectl -n cowin get pods -o wide
echo
echo "Ready: http://localhost:8080/centers  http://localhost:8080/monitor  (zone b door: :8081)"
echo "Experiments: experiments/k8s/README.md    Remove: ./deploy/k8s/down.sh"
