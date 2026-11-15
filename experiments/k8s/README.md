# E1-E3: Kubernetes experiments (commit 14)

All three run against the kind cluster from `./deploy/k8s/up.sh` and save to
`docs/evidence/14-<experiment>-<time>/` with a `results.md` and a chart.

| | Question | Run | Takes |
| --- | --- | --- | --- |
| E1 | Does the autoscaler add pods when load jumps, and remove them after? | `./experiments/e1_hpa/run_e1.sh` | 8 min |
| E2 | Does the site keep answering while pods are killed, or a whole zone goes down? | `./experiments/e2_self_heal/run_e2.sh pods` / `zone` | 3 / 5 min |
| E3 | Can we release a new version with nobody noticing? Rolling update vs stop-and-replace | `./experiments/e3_rollout/run_e3.sh rolling` / `recreate` | 2 min each |

Run them one at a time. Each script puts the cluster back the way it found it (2 pods minimum,
release v1, rolling updates).

## E1: autoscaling

k6 sends 20 requests/s for 30 s, ramps to 300 over 60 s, holds 180 s, drops back to 20 and keeps
going for 180 s. Every 5 s the script records what the autoscaler sees (average CPU) and asks for
(pods); every second, how many pods are ready.

`results.md` gives: how long after the ramp began the first new pod was asked for, the most pods,
p95 latency at the start and end of the peak, and how long it took to get back to 2 pods.

`PEAK=500 ./experiments/e1_hpa/run_e1.sh` if 300 doesn't push the CPU past 50%; check with
`kubectl -n cowin get hpa -w` in another terminal. Expect scale-down to take a minute or more:
the autoscaler waits 60 s before removing pods, then removes one every 15 s, so a short dip in
traffic doesn't throw away capacity.

## E2: self-healing

A probe sends 20 requests/s for the whole run and logs every answer (which pod, which node).

- `pods`: 4 pods; at 30, 60 and 90 s one is force-killed (no graceful shutdown, like a crash).
  Kubernetes notices and starts a replacement.
- `zone`: 4 pods over both zones; at 40 s the zone a worker is stopped (`docker stop`, like a
  zone outage), at 180 s it is started again. The probe goes through the zone b door
  (`localhost:8081`), the way the ALB keeps working from the zone that is still up.
  Kubernetes needs about 40 s to declare the node dead, then 20 s more (the toleration) before it
  moves the pods. Until then nginx's connections to pods in zone a time out after 1 s and are
  retried on another pod, so expect slow answers rather than errors.

## E3: rolling update vs recreate

4 pods on `v1`; at 20 s the release is changed to `v2` (`kubectl set env ... APP_RELEASE=v2`,
which replaces every pod). Every answer carries `X-App-Release`, so the chart shows v1 being
replaced by v2.

- `rolling`: one extra v2 pod first; an old one is removed only after the new one passes its
  readiness check. Expect no failures and a short time where both versions answer.
- `recreate`: every v1 pod is stopped, then the v2 pods start. Expect a gap of several seconds
  with no answers: that is the downtime a rolling update avoids.

## Reading the charts

E2 and E3 (`timeline.svg`): requests each second (green answered, orange answered but slower
than 1 s, red failed), ready pods by node or release, and who answered. The red dashed lines are
the events. The headline uses the same rule as the failover test: the longest stretch with no
successful answer.

E1 (`e1-chart.svg`): load planned vs done, pods asked for vs ready, average CPU against the 50%
target, and latency.

Tests for the analysis code: `pytest experiments/k8s/tests`. CI (`.github/workflows/k8s.yml`)
builds the cluster on GitHub's runner and runs E3 rolling and E2 pods on every push; the results
are attached to the run as an artifact.
