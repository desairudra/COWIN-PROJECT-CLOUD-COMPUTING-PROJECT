# E6: one table vs partitioned vs sharded (commit 16)

Does splitting registrations help, and which kind of split?

| Setup | What it is |
| --- | --- |
| one-table | 1 PostgreSQL server, registrations in one plain table (1 hash partition) |
| partitioned | 1 server, the tables split into 8 hash partitions by mobile (commit 1's default) |
| sharded-2 | 2 servers; each mobile lives on one of them (the service's `ShardedDatabase`) |
| sharded-4 | 4 servers, same routing |

Every server is a Docker container limited to **1 CPU and 1 GB**, standing in for one database
machine each. So one-table vs sharded-4 is one small machine vs four.

## Run

```bash
docker compose -f db/shards/docker-compose.yml up -d --wait     # 4 servers on ports 5441-5444
python experiments/e6_shard/run_e6.py                           # about 4 minutes
docker compose -f db/shards/docker-compose.yml down -v
```
Run it from the repo root with the venv active (it needs `psycopg` from `service/requirements.txt`).

For each setup it wipes the schema, preloads 20,000 registered mobiles, then 4 processes x 8
threads sign people up as fast as they can for 30 s: register the mobile, add one beneficiary,
list (3 database calls). It goes through the service's own database classes, so the routing and
the cross-database photo ID check are the real ones.

Options: `--duration 60`, `--procs 6 --threads 8` (more load), `--preload 100000`,
`--setups one-table,sharded-4`.

## Output

`docs/evidence/16-e6-<time>/results.md` and `e6-chart.svg`:
- sign-ups per second and database calls per second for each setup, and the ratio to one table;
- p50 / p95 / p99 latency per call, and p95 per call type (register, add, list);
- a "query over everyone" (people born before 1960): one query on one server, or one per shard
  added up. This is what sharding costs;
- how evenly the mobiles landed on each database and each partition.

## What to expect

- **Partitioned vs one table:** about the same speed. Partitions make each table and index
  smaller (cheaper vacuum, faster index maintenance, old data can be dropped a partition at a
  time) but everything still runs on one server's CPU.
- **Sharded:** close to 2x and 4x the sign-ups per second, until the laptop running the clients
  becomes the limit. Adding a beneficiary costs a little more because the photo ID is claimed on
  one database and the beneficiary written on another.
- **Spread:** within a few percent across databases and partitions.

If every setup comes out about the same, the servers are probably not CPU-limited (check with
`docker stats` during a run) or the client is the bottleneck: try `--procs 6`.
