# Registration load test (OTP, verify, add, list)

Run 2026-11-14 14:18 UTC, steps of 20 s at 2, 5, 10, 20, 40 sign-ups/s.

**Every step passed.** At the highest step, 40 sign-ups/s, p95 latency was 6 ms and 0.00% of requests failed. The limit is above what this run tried: add a higher step to RATES to find it.

| Target | Load sent | p50 ms | p95 ms | p99 ms | Max ms | Failed | Sign-ups done | Step |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2 sign-ups/s | 41 of 41 (100%) | 8 | 13 | 16 | 36 | 0 of 164 (0.00%) | 41 of 41 | pass |
| 5 sign-ups/s | 101 of 101 (100%) | 8 | 13 | 15 | 19 | 0 of 404 (0.00%) | 101 of 101 | pass |
| 10 sign-ups/s | 201 of 201 (100%) | 8 | 12 | 13 | 26 | 0 of 804 (0.00%) | 201 of 201 | pass |
| 20 sign-ups/s | 401 of 401 (100%) | 6 | 10 | 11 | 28 | 0 of 1604 (0.00%) | 401 of 401 | pass |
| 40 sign-ups/s | 801 of 801 (100%) | 4 | 6 | 8 | 25 | 0 of 3204 (0.00%) | 801 of 801 | pass |

A step passes when at least 95% of the planned load was sent, p95 is under 500 ms and under 1% of requests failed. "Load sent" below 100% means answers were so slow that k6 ran out of virtual users and had to skip some of the planned load.

Per endpoint at 40 sign-ups/s (highest step that passed):

| Endpoint | Requests | p50 ms | p95 ms | p99 ms | Failed |
| --- | --- | --- | --- | --- | --- |
| `add_beneficiary` | 801 | 4 | 7 | 8 | 0 |
| `list_beneficiaries` | 801 | 3 | 4 | 6 | 0 |
| `otp_request` | 801 | 4 | 6 | 8 | 0 |
| `otp_verify` | 801 | 4 | 7 | 8 | 0 |

![load chart](load-chart.svg)

Target: `http://localhost:8000`
Data: `per_second.csv` (one row per second), `summary.json` (k6 end-of-test summary).
