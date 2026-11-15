# Load tests (commit 13)

k6 step tests: the load goes up in steps (for example 25, 50, 100, 200, 400 requests a second,
60 s each) and we look for the first step where the service stops keeping up.

| File | What it does |
| --- | --- |
| `k6/lookup.js` | Hits the lookup service like users browsing: 50% by city, 30% by centre ID, 20% by PIN code. Counts which zone answered |
| `k6/registration.js` | Full sign-up per iteration: request OTP, verify, add a beneficiary, list. Every sign-up uses a new mobile number |
| `k6/steps.js` | Builds one k6 scenario per step (`rate_25`, `rate_50`, ...) at a fixed arrival rate |
| `run.sh` | Runs k6, then `analyze.py`; saves the results to `docs/evidence/13-load-<target>-<time>/` |
| `analyze.py` | k6 CSV output to `results.md`, `per_second.csv` and `load-chart.svg` |

Fixed arrival rate means k6 starts the planned number of requests every second whether or not
earlier ones have finished, like real users. A slow server shows up as high latency and as
load k6 could not send (dropped iterations), not as a quietly lower request rate.

## Install k6

```bash
sudo pacman -S k6            # Arch
brew install k6              # macOS
```
Other systems: https://grafana.com/docs/k6/latest/set-up/install-k6/. Without k6, `run.sh`
uses the `grafana/k6` Docker image.

## Lookup service on AWS

Build the setup first (runbook steps 1-6), then:
```bash
./loadtest/run.sh lookup                 # uses the ALB address from infra/aws/.state/alb.env
```
About 6 minutes. If every step passes, go higher: `RATES=400,800,1200 ./loadtest/run.sh lookup`.
At the end it prints an `export_graphs.sh` command that saves the CloudWatch view of the same
run (requests per zone, healthy servers, 5xx) into the results folder.

The load comes from your laptop, so the latency includes the trip to Mumbai, and at the high
steps your own connection can become the limit. Check `p50` at the first step: that is mostly
network time.

## Registration service (local)

```bash
docker compose -f db/docker-compose.yml up -d
cd service && TRUST_PROXY=1 REDIS_URL=redis://localhost:6379/0 python -m uvicorn app.main:app --port 8000
./loadtest/run.sh registration           # in a second terminal, from the repo root
```
The rate limits from commit 12 stay on. Each simulated user sends its own `X-Forwarded-For`
address, which the service only trusts with `TRUST_PROXY=1`. Behind the ALB the real address is
added by the load balancer, so this does not let anyone get around the limits there.

## Reading the results

A step **passes** when at least 95% of the planned load was sent, p95 latency is under 500 ms
(`P95_LIMIT`) and under 1% of requests failed. The headline in `results.md` gives the highest
step that passed and what went wrong at the next one.

| Setting | Default | |
| --- | --- | --- |
| `RATES` | lookup `25,50,100,200,400`, registration `2,5,10,20,40` | one step per value, per second |
| `STEP` | `60` | seconds per step |
| `P95_LIMIT` | `500` | ms |
| `CHECK` | off | `1`: exit 1 unless the first step passed (CI) |

The raw k6 output (about 20 rows per request) stays in `loadtest/.out/` and is not committed;
`per_second.csv` has everything the chart needs.

Tests: `pytest loadtest/tests`. CI (`.github/workflows/loadtest.yml`) runs them, then a short
k6 run against both services.
