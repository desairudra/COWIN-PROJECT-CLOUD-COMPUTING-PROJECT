# E4: OTP flood

Does the commit 12 protection stop OTP abuse without hurting real users?

The script starts the registration service twice, first with all limits off and then on. Each
time, for 60 seconds, three attackers and 30 real users use it at the same time:

| Actor | Behaviour | Measure |
| --- | --- | --- |
| A | One machine requests OTPs for random numbers, 20 per second | OTP texts sent (each one is an SMS someone pays for) |
| B | 50 machines request OTPs for one victim's number, 10 per second | Texts arriving on the victim's phone |
| C | One machine guesses one OTP, 10 per second | Guesses the service actually checked |
| Users | 30 people, each from their own IP: request OTP, verify, add a person, list | Sign-ups completed, refusals, time taken |

Different client IPs are simulated with `X-Forwarded-For`, which the service trusts with
`TRUST_PROXY=1` exactly as it would behind the load balancer.

## Run

Needs PostgreSQL with the commit 1 schema, and Redis. With Docker:

```bash
docker compose -f db/docker-compose.yml up -d
pip install -r service/requirements.txt
python experiments/e4_otp_flood/run_e4.py \
    --db postgresql://cowin:cowin@localhost:5432/cowin --redis redis://localhost:6379/0
```

It takes about 2.5 minutes and writes `docs/evidence/12-e4-<time>/`:
`results.md` (the comparison), `e4-chart.svg` and one CSV of every request per mode.
Options: `--duration 60 --users 30 --port 8091`.

**Redis:** the script empties the Redis database it is given before each mode. Point it at a
database used only for this (the `0` in the URL), never at shared data.

## What to expect

With limits off, every attack request succeeds. With them on:
- **A** is cut to about 100 texts plus a few as the bucket refills;
- **B**'s victim gets 1 or 2 texts;
- **C** gets 3 guesses before the OTP locks;
- **all real users still sign up**, with no refusals and a few milliseconds of extra time.

A local trial (20 s, 10 users) gave: A 400 to 106, B 200 to 1, C 200 to 3, users 10 of 10 in
both modes.
