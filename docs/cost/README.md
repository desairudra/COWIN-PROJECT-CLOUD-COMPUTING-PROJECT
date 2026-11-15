# Cost model (commit 17)

| File | What it does |
| --- | --- |
| `cost_model.py` | Prices, our measured capacities and the assumptions, all in tables at the top. Sizes the system for 475 and 741 registrations/s and writes report Section 6 to `docs/report/sec-6-cost.md` |
| `phase1_bill.sh` | Pulls what our AWS work actually cost, per service, from Cost Explorer into `docs/evidence/17-bill-<date>.json`. `cost_model.py` puts it in Section 6.6 |

```bash
./docs/cost/phase1_bill.sh          # needs ce:GetCostAndUsage on the IAM user; $0.01 per call
python docs/cost/cost_model.py      # rewrites docs/report/sec-6-cost.md
```

To change a price or an assumption, edit the table in `cost_model.py` and run it again: every
total in the section is recomputed, so the text never disagrees with the numbers.

Before quoting a figure outside the report, check it in the AWS Pricing Calculator
(https://calculator.aws). A few prices (ALB, ElastiCache, WAF) are list prices from other regions
and are marked as such.
