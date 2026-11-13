# AWS scripts

| Script | Commit | What it does |
| --- | --- | --- |
| `01_network.sh` | 4 | Creates the VPC, two public subnets (ap-south-1a, ap-south-1b), internet gateway, route table and two security groups |
| `verify_network.sh` | 4 | Checks all of the above, prints PASS/FAIL per check |
| `allow_my_ip.sh` | 4 | Temporarily opens port 8000 (and optionally 22) to your own IP, for testing servers before the load balancer exists |
| `99_teardown_network.sh` | 4 | Deletes the network again |
| `config.sh` | 4 | Shared settings (region, CIDRs, names); every script reads it |

Commits 5 (EC2 servers) and 6 (load balancer) add their scripts here and read the IDs that
`01_network.sh` saves in `.state/network.env` (not committed: it is per-account).

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

### How it was tested

The scripts were run against [moto](https://github.com/getmoto/moto), a local AWS
emulator, with the real AWS CLI. Tested: create, re-run without duplicates, all checks
passing, the check failing when port 8000 is opened to the internet, the temporary IP
rule, teardown refusing while a server runs, full teardown, and rebuilding afterwards.
An emulator is not AWS, so run `verify_network.sh` on the real account too.
