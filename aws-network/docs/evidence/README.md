# Evidence

Screenshots and command output that prove the AWS work (TA brief Steps 3 to 5), since
console work does not show up in code. One prefix per commit, so it is clear who did what:

| Prefix | Commit | Owner | Examples |
| --- | --- | --- | --- |
| `04-` | 4. VPC, subnets, security groups | Rudra | `04-verify-network.txt`, `04-subnets.png` |
| `05-` | 5. One EC2 server per AZ | Viraj | `05-instances.png`, `05-smoke-a.txt` |
| `06-` | 6. Load balancer + health checks | Harsh | `06-targets-healthy.png`, `06-distribution.txt` |
| `07-` | 7. Zone monitor page | Viraj | `07-monitor.png` |
| `08-` | 8. Zone A shutdown test | Viraj | `08-failover.mp4`, `08-timeline.png`, `08-results.md` |
| `10-` | 10. CloudWatch | Rudra | `10-dashboard.png` |
| `15-` | 15. Auto scaling + CloudFormation | Viraj | `15-replaced-instance.png` |

Keep screenshots under about 1 MB each (PNG or JPG). Crop out account IDs if you like.
Never commit access keys or `.pem` key files.
