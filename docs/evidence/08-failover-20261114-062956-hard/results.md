# Failover test (hard stop of Zone A)

Run `20261114-062956-hard` against `http://cowin-it457-alb-1251153005.ap-south-1.elb.amazonaws.com/health`: 600 requests at 5 per second over 120 s. Zone A's server (`i-09d9308771b77178b`, ap-south-1a) was stopped at t = 30 s.

**PASS: the site stayed up.** After the stop, 428 of 449 requests succeeded (95.3%). The longest stretch with no successful answer was 0.6 s, so a user retrying was never locked out for longer than that.

| Measure | Value |
| --- | --- |
| Requests before the stop: ap-south-1a / ap-south-1b / failed | 76 / 74 / 1 |
| Requests after the stop | 449 |
| Failed after the stop | 21 (4.7%) |
| Longest stretch with no successful answer | 0.6 s |
| Failure codes | 502 x16, TimeoutError x5 |
| First failure after the stop | 2.4 s |
| Last failure after the stop | 10.2 s |
| Last answer from ap-south-1a after the stop | 1.6 s |
| Load balancer marked Zone A unhealthy after | 10.7 s |
| Slowest successful answer after the stop | 6045 ms |
| Availability over the whole run | 96.3% |

![timeline](timeline.svg)

Zone A target states seen by the load balancer (t in seconds from the start):

- t = 0.0 s: healthy
- t = 40.8 s: unhealthy
- t = 51.0 s: unused

Raw data: `probe.csv` (every request), `health.csv` (target health every 2 s), `run.env`.
