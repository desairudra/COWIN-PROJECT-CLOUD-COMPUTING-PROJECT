# Report additions for Sec 4, 5 and 7 (commit 2)

Draft text for the final report. Each block adds a subsection to the section it names; the
Week 2 subsections stay as they are. The diagram is `docs/architecture/national-architecture.png`.

---

## 4.6 Our design: the registration module across two regions

We designed the registration module to run in both Indian AWS regions, Mumbai (ap-south-1)
and Hyderabad (ap-south-2), each with three Availability Zones (Figure X). Route 53 sends
each user to the nearer healthy region using latency-based routing. In each region, AWS WAF
filters bot floods, an Application Load Balancer spreads requests across stateless servers
in all three zones, ElastiCache holds OTPs and rate-limit counters, and an SQS queue hands
OTP messages to SNS so a slow SMS never blocks a sign-up.

Registration data is sharded by mobile number. Each shard has one writer, its primary in
Mumbai, with a synchronous standby in a second Mumbai zone and an asynchronous replica in
Hyderabad. Reads are served in the user's own region; writes from either region go to the
Mumbai primary. A single writer per shard keeps the "four per mobile" and "one photo ID once"
rules exact without cross-region locking. Photo ID uniqueness lives in a separate registry
routed by photo ID, because a mobile-number shard cannot see IDs held on other shards.

## 5.5 How the design scales

Every layer above the database scales out: Route 53 and WAF are managed services, the load
balancer scales with traffic, and the app servers are stateless, so the Auto Scaling group
adds servers within the region during a surge. The target is the 28 April 2021 registration
rate of about 475 to 740 per second (Sec 9.2). Experiments E1 and E2 measure registrations
per second per server, which gives the number of servers needed for that rate.

The database scales in two steps: partitioning inside one server (commit 1), then shards
across servers (commit 16, experiment E6). Reads, which were 40 to 50 times more frequent
than completed registrations on 28 April (Sec 5.2), are served from the local region and
can be cached, so they do not load the write primaries.

## 7.6 Reliability targets

We chose these targets for the registration module:

| Failure | Recovery time | Data loss |
| --- | --- | --- |
| One server | about 10 s | none |
| One Availability Zone | under 2 minutes | none |
| The Mumbai region | reads in about 2 minutes, writes within 15 minutes | under 1 minute of registrations |

Data inside a region is copied synchronously, so a zone failure loses nothing. Copying
synchronously between regions would add a cross-region round trip to every write on a surge
day, so cross-region replication is asynchronous and a regional disaster may lose the last
seconds of writes. Registration is idempotent, so a citizen whose request was lost retries
without creating a duplicate. Promoting Hyderabad is a deliberate runbook step rather than
an automatic switch, to avoid two regions accepting writes at once.

We tested the first row on AWS: two servers in two zones behind a load balancer, with the
Zone A server shut down while the site was in use (commit 8, experiment E5). Results: see
`docs/evidence/`.

Compared with CoWIN in 2021, which most likely ran in Mumbai alone because Hyderabad opened
only in November 2022 (Sec 4.4), this design survives the loss of a whole region.
