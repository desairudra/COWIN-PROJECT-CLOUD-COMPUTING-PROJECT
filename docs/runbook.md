# Runbook

How to rebuild the AWS setup, run the demo, and tear it down. Everything runs from the repo
root with the scripts in `infra/aws/`. Details for each script are in
[`infra/aws/README.md`](../infra/aws/README.md).

## Before you start (once per laptop)

1. **AWS CLI v2** installed, or use AWS CloudShell, which has it already.
2. **An IAM user** (not root) with these policies: `AmazonEC2FullAccess`,
   `ElasticLoadBalancingFullAccess`, `AmazonSSMReadOnlyAccess`, `CloudWatchFullAccess`.
3. `aws configure` with that user's access key, region `ap-south-1`, output `json`.
4. Check: `aws sts get-caller-identity` shows `user/<your-user>`.
5. `chmod +x infra/aws/*.sh` (needed again after copying files from Windows or a zip).

Never commit access keys, `.pem` files or `infra/aws/.state/`.

## Build (about 10 minutes)

| # | Command | Takes | Done when |
| --- | --- | --- | --- |
| 1 | `./infra/aws/01_network.sh` | 30 s | `Done. IDs saved` (says `reusing` if the network already exists) |
| 2 | `./infra/aws/verify_network.sh` | 15 s | `All checks passed.` (if one route check fails right after creation, rerun) |
| 3 | `./infra/aws/05_servers.sh` | 1 min | Both servers `running` |
| 4 | Wait | 3 min | The servers install the app on first boot |
| 5 | `./infra/aws/06_load_balancer.sh` | 3-5 min | Prints the live URL |
| 6 | `./infra/aws/verify_alb.sh` | 30 s | `All checks passed.` |

The live URL is printed by step 5 and saved in `infra/aws/.state/alb.env`. It stays the same as
long as the load balancer is not deleted. If it changes, update the URL at the top of
`README.md`.

To check the servers directly before step 5 (optional):
`./infra/aws/allow_my_ip.sh add`, `./infra/aws/verify_servers.sh`, then always
`./infra/aws/allow_my_ip.sh remove`.

## Demo (about 8 minutes, for the viva)

Build first, at least 15 minutes before the slot. Then:

1. **Design (1 min).** Open `docs/architecture/national-architecture.png`. Walk one request:
   user, Route 53, the nearer region, load balancer, a server in one of three zones, the database.
   Point at the Hyderabad replicas: if Mumbai fails, Route 53 sends everyone there.
2. **The service (1 min).** Open `<live URL>/centers?city=Hyderabad` and `<live URL>/docs`.
   Point at `served_by` in the response: it names the zone that answered.
3. **Load balancing (1 min).** Open `<live URL>/monitor`. Blue and green alternate: requests
   are split between ap-south-1a and ap-south-1b, 0 failed.
4. **Failover (4 min).** Keep `/monitor` visible and run, in a terminal beside it:
   ```bash
   DURATION=120 KILL_AT=30 ./infra/aws/08_failover_test.sh hard
   ```
   - At 30 s: "server-a stop requested". The strip shows a few red ticks, then turns all green.
   - Point at "Longest outage" on the monitor page: under a second. The site never went down.
   - The terminal prints the results table. Zone A comes back by itself at the end.
5. **Graceful comparison (1 min).** Show `results.md` from the graceful run in
   `docs/evidence/`: same server lost, 0 failures caused, because it was drained first.

**If the build is not ready in time:** show the recording `docs/evidence/08-failover.mp4` and
the two `results.md` files instead.

## Tear down (stop the bill)

The load balancer and the servers are billed every hour, even with no traffic.

```bash
./infra/aws/97_teardown_alb.sh       # load balancer and target group
./infra/aws/98_teardown_servers.sh   # both servers
./infra/aws/99_teardown_network.sh   # network (free; optional, may need a few minutes after 97)
```

Check in the EC2 console that no `cowin-it457` instances or load balancers remain.

## When something goes wrong

| Symptom | Fix |
| --- | --- |
| `No .state/network.env` | `.state/` was lost. Rerun the build steps in order: they reuse what exists and rewrite it |
| `UnauthorizedOperation` | The IAM user is missing a policy from the list above |
| `Targets not healthy` in step 5 | The servers have not finished installing: `./infra/aws/05_servers.sh logs a`, wait, rerun step 5 |
| Live URL does not open | Use `http://`, not `https://`; a new load balancer's address takes 1-2 minutes to work |
| Failover test refuses to start | A target is not healthy, often right after a previous run: wait a minute, `verify_alb.sh` |
| Changed `lookup/` code | `98_teardown_servers.sh --yes`, `05_servers.sh`, wait 3 min, `06_load_balancer.sh`: same URL, new code |
| Teardown refuses | Delete in order 97, 98, 99; wait a few minutes between 97 and 99 |
