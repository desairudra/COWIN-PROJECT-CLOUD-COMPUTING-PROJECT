# Containers, nginx and Kubernetes 

Two ways to run the project in containers on one laptop:

| | What runs | Open |
| --- | --- | --- |
| `compose/` | The whole site: nginx, 2 x registration service, 2 x lookup, PostgreSQL, Redis | http://localhost:8090 |
| `k8s/` | A 3-node Kubernetes cluster (kind) with the lookup service, nginx and an autoscaler | http://localhost:8080 |

The Dockerfiles are `lookup/Dockerfile` and `service/Dockerfile` (build that one from the repo root).

## Install (Arch)

```bash
sudo pacman -S docker docker-compose kind kubectl
sudo systemctl start docker
```
Other systems: Docker from docs.docker.com, kind from https://kind.sigs.k8s.io/docs/user/quick-start/,
kubectl from https://kubernetes.io/docs/tasks/tools/.

## The whole site with Docker Compose

```bash
docker compose -f deploy/compose/docker-compose.yml up -d --build --wait
```
The first build takes a few minutes. Then http://localhost:8090 is the sign-up site (demo OTP shown
on the page), http://localhost:8090/centers the lookup service, `/monitor` the zone monitor.

nginx (`compose/nginx.conf`) sends `/centers`, `/health` and `/monitor` to the lookup containers and
everything else to the registration containers, round robin. If a container refuses the connection,
nginx tries the next one (only before the request was sent, so a sign-up is never sent twice).
The registration service runs with `TRUST_PROXY=1`: it takes the client address nginx adds, so a
client can't get around the rate limits by sending its own `X-Forwarded-For`.

```bash
docker compose -f deploy/compose/docker-compose.yml ps
docker compose -f deploy/compose/docker-compose.yml down -v      # stop and delete the data
```
Port 8090 so it doesn't clash with `db/docker-compose.yml` or a service on 8000.

## Kubernetes with kind

```bash
./deploy/k8s/up.sh       # about 3 minutes the first time
./deploy/k8s/down.sh     # delete the cluster
```

| Piece | File | |
| --- | --- | --- |
| Cluster | `k8s/kind-cluster.yaml` | control plane + 2 workers labelled `ap-south-1a` and `ap-south-1b` |
| Lookup | `k8s/lookup.yaml` | Deployment, Service, HPA (2-8 pods at 50% CPU), PodDisruptionBudget |
| Front door | `k8s/nginx.yaml` | nginx on each worker; `localhost:8080` is zone a, `localhost:8081` zone b |

How it maps to the AWS setup:

| AWS (commits 4-8) | kind |
| --- | --- |
| Two Availability Zones | Two worker nodes, labelled with the zone names |
| EC2 instance per zone | Lookup pods, spread across the zones (topology spread) |
| ALB with a node in each zone | nginx on each worker, one host port each |
| Target group health check | Readiness probe on `/health` every 2 s |
| Replace a failed server: manual | Kubernetes restarts or reschedules pods on its own |
| Fixed 2 servers | Autoscaler: 2 to 8 pods on CPU |

Details that matter for the experiments:
- Pods are kept evenly spread over the two zones (at most one apart). A zone whose node is down
  doesn't count, so during an outage the replacements can all start in the other zone. Only
  pods of the same version are counted, so a rolling update doesn't leave them all in one zone.
- Pods tolerate an unreachable node for 20 s (default 300 s), so after a zone goes down its pods
  are rescheduled in about a minute instead of five.
- Each pod waits 5 s in `preStop` before shutting down, so the Service stops sending it new
  requests before it stops answering.
- Rolling updates add one new pod first and only remove an old one once the new one is ready
  (`maxSurge 1`, `maxUnavailable 0`).
- nginx opens a new connection per request, so kube-proxy spreads requests across pods. The
  upstream is listed twice so a failed connection is retried once on a fresh one.
- CPU request 100m, limit 250m per lookup pod: small on purpose, so a laptop can push it past the
  autoscaler's target.

The experiments are in [`experiments/k8s/README.md`](../experiments/k8s/README.md).

The community ingress-nginx controller was retired in March 2026, so the front door here is plain
nginx in a Deployment instead of an Ingress controller.
