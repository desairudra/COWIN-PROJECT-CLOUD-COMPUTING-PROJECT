# AWS scripts

| Script | Commit | What it does |
| --- | --- | --- |
| `01_network.sh` | 4 | Creates the VPC, two public subnets (ap-south-1a, ap-south-1b), internet gateway, route table and two security groups |
| `verify_network.sh` | 4 | Checks all of the above, prints PASS/FAIL per check |
| `allow_my_ip.sh` | 4 | Temporarily opens port 8000 (and optionally 22) to your own IP, for testing servers before the load balancer exists |
| `99_teardown_network.sh` | 4 | Deletes the network again |
| `05_servers.sh` | 5 | Launches one EC2 server in each AZ running the lookup service |
| `user-data.sh` | 5 | The startup script each server runs on first boot (template) |
| `verify_servers.sh` | 5 | Checks both servers, AWS side and HTTP, PASS/FAIL |
| `98_teardown_servers.sh` | 5 | Terminates the servers |
| `06_load_balancer.sh` | 6 | Creates the Application Load Balancer, target group and listener: the live URL |
| `verify_alb.sh` | 6 | Checks the load balancer, health checks, targets and live URL, PASS/FAIL |
| `distribution_test.sh` | 6 | Sends 200 requests and counts how many each zone answered |
| `97_teardown_alb.sh` | 6 | Deletes the load balancer and target group |
| `10_monitoring.sh` | 10 | CloudWatch dashboard and four alarms |
| `verify_monitoring.sh` | 10 | Checks the dashboard, alarms and live metrics, PASS/FAIL |
| `export_graphs.sh` | 10 | Saves CloudWatch graphs as PNGs, e.g. around a failover run |
| `96_teardown_monitoring.sh` | 10 | Deletes the dashboard, alarms and email topic |
| `config.sh` | 4 | Shared settings (region, CIDRs, names); every script reads it |

Scripts save the IDs they create in `.state/` (not committed: it is per-account), and later
scripts read them. Tear down in reverse order: `96` (monitoring), `97` (load balancer), `98` (servers), `99` (network).

## Commit 4: the network

```
VPC 10.0.0.0/16  (ap-south-1, Mumbai)
├── subnet A  10.0.1.0/24  ap-south-1a ┐ public: routed to the internet
├── subnet B  10.0.2.0/24  ap-south-1b ┘ through one internet gateway
├── security group "alb"  port 80 from anywhere              -> the load balancer (commit 6)
└── security group "app"  port 8000 only from the "alb" group -> the EC2 servers (commit 5)
```

The "app" rule is the important one: users can never reach a server directly, only
through the load balancer. So when Zone A's server is shut down in commit 8, the load
balancer is the only thing that has to react.

All of these resources are free. AWS charges nothing for a VPC, subnets, route tables,
internet gateways or security groups. Charges start with the EC2 servers and load balancer
in commits 5 and 6.

### Run it

The easiest place is **AWS CloudShell**, the terminal icon at the top of the AWS console.
It already has the AWS CLI, bash, git and your login. Set the console region to
**Asia Pacific (Mumbai)** first.

```bash
git clone <your repo URL> && cd <repo>
./infra/aws/01_network.sh
./infra/aws/verify_network.sh | tee docs/evidence/04-verify-network.txt
```

From your own laptop instead: install the AWS CLI, run `aws configure` with an access key,
and use Git Bash or WSL on Windows.

`01_network.sh` is safe to run again: anything already there (found by its `Name` tag) is
reused, not duplicated. `verify_network.sh` should end with `All checks passed.`

### Evidence to save in `docs/evidence/` (part of this commit)

- [ ] `04-verify-network.txt`: the output of `verify_network.sh`
- [ ] `04-vpc.png`: VPC console, *Your VPCs*, showing `cowin-it457-vpc`
- [ ] `04-subnets.png`: *Subnets*, filtered to the VPC, showing the two subnets with their Availability Zones (1a and 1b)
- [ ] `04-route-table.png`: the route table's *Routes* tab showing `0.0.0.0/0 -> igw-...`
- [ ] `04-security-groups.png`: the `app` group's *Inbound rules* showing port 8000 from the `alb` group only

Crop out your account ID if you like.

### Testing servers directly (commit 5)

Before the load balancer exists, you can only reach a server by opening it to your own IP:

```bash
./infra/aws/allow_my_ip.sh add        # port 8000 from your current IP
./infra/aws/allow_my_ip.sh add-ssh    # plus SSH
./infra/aws/allow_my_ip.sh remove     # always run this when done
```

`verify_network.sh` prints a `NOTE` while any such rule exists, and `FAIL`s if port 8000 is
ever open to the whole internet.

### Tear down

```bash
./infra/aws/99_teardown_network.sh
```

It refuses to run while servers or a load balancer still exist in the VPC (tear down
commits 6 and 5 first). It finds resources by tag, so it works even if `.state/` is gone.

### Settings

Everything is in `config.sh` and can be overridden per run, e.g.
`PROJECT=cowin-team7 ./infra/aws/01_network.sh` if two teams share an account.

### Troubleshooting

| Message | Fix |
| --- | --- |
| `AWS CLI is not logged in` | Run `aws configure`, or use CloudShell |
| `Availability Zone ap-south-1a is not available` | Your account maps zone names differently; set `AZ_A`/`AZ_B` to two zones listed by `aws ec2 describe-availability-zones` |
| `VpcLimitExceeded` | The region already has 5 VPCs. Delete an unused one, or reuse via `PROJECT=` |
| `Permission denied` running a script | `chmod +x infra/aws/*.sh`, or run with `bash infra/aws/01_network.sh` |

### How commit 4 was tested

The scripts were run against [moto](https://github.com/getmoto/moto), a local AWS
emulator, with the real AWS CLI. Tested: create, re-run without duplicates, all checks
passing, the check failing when port 8000 is opened to the internet, the temporary IP
rule, teardown refusing while a server runs, full teardown, and rebuilding afterwards.
An emulator is not AWS, so run `verify_network.sh` on the real account too.

## Commit 5: one server per Availability Zone

```
subnet A (ap-south-1a) ── server-a: Amazon Linux 2023, t3.micro, lookup service on :8000
subnet B (ap-south-1b) ── server-b: same
both in the "app" security group: port 8000 reachable only from the load balancer
```

How a server gets its app: `05_servers.sh` packs `lookup/app` and `lookup/requirements.txt`
into the startup script (user data). On first boot each server unpacks it, builds a Python
virtualenv, installs FastAPI and uvicorn, and runs the app under systemd as an unprivileged
user (restarted on crash or reboot). The servers never need GitHub, so this works with a
private repo, and they run exactly the code you launched from. Run `05_servers.sh render`
to see the generated script.

Each server also requires IMDSv2 for its metadata, which is how the app learns its own
instance ID and zone (`served_by` in every response).

### Run it

```bash
./infra/aws/05_servers.sh                 # launches server-a and server-b (about 1 minute)
# wait 1-3 minutes for the app to install, then:
./infra/aws/allow_my_ip.sh add            # let your computer reach port 8000
./infra/aws/verify_servers.sh | tee docs/evidence/05-verify-servers.txt
./infra/aws/allow_my_ip.sh remove         # close it again
```

`verify_servers.sh` runs 11 AWS-side checks (state, zone, subnet, security group, IMDSv2,
different zones) and 8 HTTP checks (health, own zone, own instance ID, 20 centres, per
server). It should end with `All checks passed.` You can also open
`http://<IP_A>:8000/centers` in a browser while your IP is allowed.

Other commands: `05_servers.sh status` (table of both servers), `05_servers.sh logs a`
(boot log of server-a; can lag a few minutes behind).

### Cost

The servers are billed while they run. AWS also bills each public IPv4 address by the hour.
`t3.micro` is a small, low-cost type; check whether your account's free tier covers it, and
use `INSTANCE_TYPE=t2.micro ./infra/aws/05_servers.sh` if your free tier lists t2.micro instead.
Tear down when you are not working on it:

```bash
./infra/aws/98_teardown_servers.sh        # servers (keeps the free network)
```

### Evidence to save in `docs/evidence/` (part of commit 5)

- [ ] `05-verify-servers.txt`: the output of `verify_servers.sh`, with HTTP checks
- [ ] `05-instances.png`: EC2 console, *Instances*, both servers *Running*, with the
      *Availability Zone* column showing ap-south-1a and ap-south-1b
- [ ] `05-server-a.png` and `05-server-b.png`: browser at `http://<IP>:8000/health`, showing
      each server's own `availability_zone`

### Troubleshooting

| Problem | Fix |
| --- | --- |
| `did not answer on port 8000` | Did you run `allow_my_ip.sh add`? If yes, the app may still be installing: wait 2 minutes, then `05_servers.sh logs a` |
| Boot log shows `did NOT start` | Connect with EC2 Instance Connect (needs `allow_my_ip.sh add-ssh`) and run `sudo journalctl -u cowin-lookup` |
| `InsufficientInstanceCapacity` or `Unsupported` | Try `INSTANCE_TYPE=t3a.micro` or `t2.micro` |
| `VcpuLimitExceeded` | New accounts have a low vCPU limit; terminate other instances |
| Changed `lookup/` and want it on the servers | `98_teardown_servers.sh --yes` then `05_servers.sh` (servers are replaceable by design) |

### How commit 5 was tested

On the moto emulator: launch, re-run without duplicates, all 11 AWS-side checks, user data
stored on the instance, the network teardown refusing while servers run, and both
teardowns. The HTTP checks ran against two real copies of the app on 127.0.0.1 and
127.0.0.2 posing as the two servers: all passed, and a wrong zone and a stopped server were
both reported as FAIL. The generated startup script was run end to end on Linux (unpack,
virtualenv, install, start, smoke test: all passed), and its systemd unit passed
`systemd-analyze verify`. Not tested: an actual EC2 boot. That is the first thing to check
on the real account.

## Commit 6: the load balancer (the live URL)

```
              internet, port 80
                     │
   ALB cowin-it457-alb, in subnet A and subnet B, security group "alb"
                     │  listener HTTP :80 -> target group
   target group cowin-it457-tg: HTTP :8000, health check GET /health
          ┌──────────┴──────────┐
   server-a (ap-south-1a)  server-b (ap-south-1b)
```

The load balancer is the only way in: the servers' "app" security group accepts port 8000
from the "alb" group and nothing else (commit 4). It sends each request to a healthy server,
alternating between them.

Health-check settings are tuned for the failover demo in commit 8:

| Setting | Value | AWS default | Why |
| --- | --- | --- | --- |
| Path | `/health` | `/` | The lookup service's cheap health endpoint |
| Interval | 5 s | 30 s | Notice a dead server quickly |
| Timeout | 2 s | 5 s | Must be shorter than the interval |
| Unhealthy after | 2 failures | 2 | So a dead server stops getting traffic within about 10 s |
| Healthy after | 2 passes | 5 | A recovered server rejoins within about 10 s |
| Deregistration delay | 10 s | 300 s | A removed server is drained in 10 s instead of 5 minutes |

### Run it

Needs the network (commit 4) and both servers (commit 5) running.

```bash
./infra/aws/06_load_balancer.sh        # 3-5 minutes; prints the live URL at the end
./infra/aws/verify_alb.sh | tee docs/evidence/06-verify-alb.txt
./infra/aws/distribution_test.sh | tee docs/evidence/06-distribution.txt
```

`verify_alb.sh` runs 18 checks (load balancer, both zones, listener, the six health-check
settings, both targets healthy, the live URL answering, and the servers not being reachable
directly). `distribution_test.sh` should show close to 50/50 between ap-south-1a and
ap-south-1b with 0 failed requests, and end with `RESULT: PASS`.

The live URL is `http://<ALB DNS name>/centers` (plain HTTP; there is no domain or
certificate in this project). It is saved in `.state/alb.env` and printed by `verify_alb.sh`.
Try `/centers?city=Hyderabad`, `/centers/MUM-001`, `/health` and `/docs`, and refresh
`/health` a few times to watch `availability_zone` change.

### Evidence to save in `docs/evidence/` 

- [ ] `06-verify-alb.txt` and `06-distribution.txt`: the two outputs above
- [ ] `06-alb.png`: EC2 console, *Load Balancers*, `cowin-it457-alb` *Active*, with its two Availability Zones
- [ ] `06-targets-healthy.png`: *Target Groups*, `cowin-it457-tg`, *Targets* tab, both **healthy**, one per zone
- [ ] `06-health-check.png`: the target group's *Health checks* tab with the settings above
- [ ] `06-live-url.png`: a browser on `http://<ALB DNS name>/centers`

### Cost

A load balancer is billed by the hour plus a usage charge, whether or not anyone uses it.
After the demo or evidence session, tear everything down in this order:

```bash
./infra/aws/97_teardown_alb.sh         # load balancer
./infra/aws/98_teardown_servers.sh     # servers
./infra/aws/99_teardown_network.sh     # network (free, optional; may need a few minutes after 97)
```

Rebuilding later is `05_servers.sh` then `06_load_balancer.sh` (about 8 minutes). The live URL
changes when the load balancer is recreated.

### Troubleshooting

| Problem | Fix |
| --- | --- |
| `Targets not healthy` | The app is not answering on the servers. Check `./infra/aws/05_servers.sh logs a`; if the servers were just launched, wait 3 minutes and rerun `06_load_balancer.sh` |
| Live URL does not open | A new load balancer's DNS name takes 1-2 minutes to resolve. Use `http://`, not `https://` |
| `AccessDenied` on `CreateLoadBalancer` | Attach `ElasticLoadBalancingFullAccess` to your IAM user. The first load balancer in an account also creates a service-linked role, which that policy allows |
| `distribution_test.sh`: only one zone answered | One target is unhealthy: see the *Targets* tab |
| `99_teardown_network.sh` refuses after `97` | The load balancer's network interfaces take a few minutes to disappear; wait and rerun |

### How commit 6 was tested

On the moto emulator: create, re-run without duplicates, all AWS-side checks, replacing
the servers (targets updated to the new ones), and teardown in order 97, 98, 99. The HTTP
side ran through a local round-robin proxy standing in for the load balancer, with two
copies of the app as the two zones. An even split passed at 50/50; an 80/20 split and a
dead zone (50 failed requests) were both reported as FAIL. The live-URL check and the
"servers not directly reachable" check were tested both ways. Not tested: a real AWS load
balancer, so check the health-check settings in the console after the first real run.

## Commit 7: zone monitor page

`http://<ALB DNS name>/monitor` polls the load balancer twice a second and shows, per request,
which zone answered: a blue/green strip, counts per zone, failed requests, the longest outage
and a request log. **Download CSV** saves every request for commit 8.

The page is part of the lookup service, so the servers need the new code. The load balancer
and its URL stay the same:

```bash
./infra/aws/98_teardown_servers.sh --yes
./infra/aws/05_servers.sh              # wait 2-3 minutes
./infra/aws/06_load_balancer.sh        # registers the new servers
```

Then open `/monitor`: the strip should alternate between the two zones with 0 failed.

Options: `?target=/centers` polls another path; the dropdown changes the rate (1, 2 or 4 per second).

Evidence for `docs/evidence/`: `07-monitor.png`, the page after a minute of normal traffic,
showing both zones and 0 failed.

## Commit 8: Zone A shutdown test (E5)

The proof for Step 5: while the live site is being used, stop the server in Zone A and
measure what users see.

```bash
./infra/aws/08_failover_test.sh hard        # stop server-a without warning
./infra/aws/08_failover_test.sh graceful    # remove it from the load balancer first, then stop it
```

Each run takes about 5 minutes:
- probes the live URL 5 times a second for 180 s;
- stops server-a at 60 s;
- records both targets' health every 2 s;
- starts server-a again and re-registers it.

Results go to `docs/evidence/08-failover-<time>-<mode>/`:

| File | What it is |
| --- | --- |
| `results.md` | Verdict and the numbers: failed requests, longest stretch with no successful answer, when the load balancer noticed |
| `timeline.svg` | Every request on a timeline, one row per zone plus failures |
| `probe.csv` | Every request: time, status, zone, instance, latency |
| `health.csv` | The load balancer's view of each target every 2 s |

**What "no downtime" means here:** the verdict is PASS when the longest stretch with no
successful answer stays under 5 seconds, so a user who retried was never locked out.

**Expected results:**
- **Hard stop:** a short burst of failed requests (about half the traffic for roughly 10 s),
  because the load balancer only stops using server-a after 2 failed health checks 5 s apart.
- **Graceful stop:** 0 failed, because server-a is drained before it goes down.

Report both: the difference is what health checks and connection draining are for.

### Recording the demo

1. Open `http://<ALB DNS name>/monitor` and start a screen recording.
2. Run `./infra/aws/08_failover_test.sh hard` in a terminal next to it.
3. At 60 s the strip turns all green, with a few red ticks. Keep recording until the end.
4. Save the recording as `docs/evidence/08-failover.mp4`. If it is over about 50 MB, upload it
   to Google Drive and put the link in `docs/evidence/README.md` instead.

Settings: `DURATION=300 KILL_AT=120 RATE=10 ./infra/aws/08_failover_test.sh hard`.
`SKIP_RESTORE=1` leaves server-a stopped.

### How commit 8 was tested

The full script ran on the moto emulator, with the prober pointed at a local stand-in load
balancer and the zone A app copy really killed at the stop moment. Hard mode gave
4 failed requests with a 0.4 s worst gap (PASS). Graceful mode gave 0 failed (PASS). A
synthetic 8 s blackout was reported as FAIL. Those test outputs are not in the repo: only
real AWS runs belong in `docs/evidence/`.

## Commit 10: CloudWatch dashboard and alarms

AWS's own view of the load balancer, next to the probe's view from commit 8.

```bash
./infra/aws/10_monitoring.sh
./infra/aws/verify_monitoring.sh | tee docs/evidence/10-verify-monitoring.txt
```

**Dashboard `cowin-it457`** (CloudWatch console, *Dashboards*; the script prints the link):

| Graph | Shows |
| --- | --- |
| Healthy servers per zone | 1 per zone normally; zone A drops to 0 when it is stopped |
| Requests per zone | The load balancer's even split, then all traffic on zone B |
| Errors per minute | 5xx from the load balancer (server unreachable) vs from the app |
| Response time | Median and 99th percentile |
| Server CPU | Per server |
| Alarms | Current state of the four alarms |

**Alarms** (1-minute periods):

| Alarm | Fires when |
| --- | --- |
| `cowin-it457-healthy-hosts-low` | Fewer than 2 healthy servers |
| `cowin-it457-zone-a-down` | No healthy server in ap-south-1a |
| `cowin-it457-zone-b-down` | No healthy server in ap-south-1b |
| `cowin-it457-elb-5xx` | 5 or more load balancer 5xx errors in a minute |

To also get an email: `ALARM_EMAIL=you@example.com ./infra/aws/10_monitoring.sh`, then click the
confirmation link AWS sends. This needs `AmazonSNSFullAccess` on your IAM user as well.

Load balancer metrics arrive once a minute, so right after setup the alarms show
`INSUFFICIENT_DATA` and `verify_monitoring.sh` asks you to wait 2-3 minutes. Rerun
`10_monitoring.sh` after replacing the servers, so the CPU graph follows the new ones.

### Graphs of a failover run

```bash
./infra/aws/export_graphs.sh --run docs/evidence/08-failover-<run>
```

This saves three PNGs into that run's folder, straight from CloudWatch:
`10-<mode>-healthy-hosts.png`, `10-<mode>-requests.png` and `10-<mode>-errors.png`. Each covers 3
minutes before to 7 minutes after the run, with red lines where zone A was removed and stopped.
CloudWatch keeps these metrics for 15 days, so runs that are already done can be exported too,
as long as the load balancer has not been deleted and recreated since.

Without `--run`, it exports the last 30 minutes into `docs/evidence/`.

### Evidence for `docs/evidence/` (part of commit 10)

- [ ] `10-verify-monitoring.txt`
- [ ] The three exported PNGs for the hard run and the graceful run (inside their `08-failover-*` folders)
- [ ] `10-dashboard.png`: a screenshot of the whole dashboard during or just after a failover run
- [ ] `10-alarm.png`: the `zone-a-down` alarm in *In alarm* during a hard stop (*Alarms* page)

### Cost

Dashboards and alarms are billed per month, prorated, and a few cents for this setup.
Remove them with `./infra/aws/96_teardown_monitoring.sh` (run it before `97`).

### How this was tested

On the moto emulator: alarms and dashboard created, re-run without duplicates, the email topic
and alarm actions, every `verify_monitoring.sh` check (failing with no metrics yet, passing
after metric data was added), and teardown. The graph definitions were checked against
CloudWatch's widget format and a sample failover run (time window and red markers correct).
The emulator cannot draw graph images, so `export_graphs.sh` is only proven on the real account.
