# The AWS setup as one CloudFormation stack (commit 15)

Commits 4-6 build the setup with AWS CLI scripts, one piece at a time. This builds the same thing
from one template, `cowin-stack.yaml`, and adds an **Auto Scaling group** in place of the two
hand-made servers:

| | CLI scripts (commits 4-6) | This stack |
| --- | --- | --- |
| Build | 3 scripts in order | `./infra/cfn/stack.sh up` |
| Delete | 3 teardown scripts in reverse order | `./infra/cfn/stack.sh down` |
| Servers | 2 fixed instances | Auto Scaling group: 2 to 4, spread over both zones, from a launch template |
| A server dies | stays dead until someone relaunches it | the group replaces it on its own |
| More load | nothing changes | adds servers to keep average CPU near 50% |
| Network, ALB, health checks | VPC 10.0.0.0/16, `cowin-it457-*` | same design, VPC 10.1.0.0/16, `cowin-cfn-*` |

The two setups don't share anything, so either can run without the other (but there's no reason
to pay for both at once).

## Before the first run: permissions

CloudFormation and Auto Scaling need two more policies on the IAM user (`desairudra`). IAM →
Users → desairudra → Add permissions → Attach policies directly:
- `AWSCloudFormationFullAccess`
- `AutoScalingFullAccess`

## Use

```bash
./infra/cfn/stack.sh check     # AWS validates the template (free, 5 s)
./infra/cfn/stack.sh up        # about 5 min, then 2-4 min for the servers to install the app
./infra/cfn/stack.sh status    # servers, zones, health
./infra/cfn/replace_test.sh    # stop a server, watch it come back (8 min)
./infra/cfn/stack.sh down      # deletes every resource in the stack (about 5 min)
```

`up` puts the lookup app inside the startup script (the same script as commit 5, from
`05_servers.sh render`), drops it into the template where `__USER_DATA__` is, and deploys that.
Run `up` again after changing the app: CloudFormation makes a new launch template version and the
group replaces the servers one at a time, keeping at least one in service.

What it costs while it runs: 2 x t3.micro and the ALB, about ₹5 an hour. Run `down` when done.

## The replacement test

`replace_test.sh` probes the site 5 times a second for 8 minutes. At 60 s it stops the server in
zone a (`aws ec2 stop-instances`, like a server that crashed). It records every 5 s which servers
the group has and whether the load balancer calls them healthy, and logs:
- when the group marks the stopped server unhealthy,
- when it launches a replacement (in the zone that is now short of a server),
- when the replacement passes its health checks.

Results go to `docs/evidence/15-replace-<time>/` with `results.md` and `timeline.svg`, made by the
same analysis as the Kubernetes experiments (`experiments/k8s/analyze.py`).

Expect no failed requests (the load balancer stops sending traffic to the stopped server within
about 10 s, and the zone b server takes all of it), and the replacement healthy 3-5 minutes after
the stop. Most of that is the new server booting and installing Python packages.

## When something fails

- `up` prints the first failed resources and the reason. A stack that failed on its first create
  is deleted automatically the next time you run `up`.
- `down` fails if something outside the stack was put in the VPC by hand; delete that, run `down` again.
- Console: CloudFormation → Stacks → `cowin-it457-cfn` → Events shows each step.
