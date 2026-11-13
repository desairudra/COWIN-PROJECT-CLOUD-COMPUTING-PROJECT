# Vaccine Center Lookup microservice

A  very simple microservice that returns static JSON. It lists 20
made-up vaccination centres, 10 in Mumbai and 10 in Hyderabad. Commits 5 to 8 deploy it on
two EC2 servers in two Availability Zones, put a load balancer in front, and shut one zone
down to test failover.

| Endpoint | Returns |
| --- | --- |
| `GET /` | Service name, version, endpoint list |
| `GET /health` | `{"status": "ok", ...}`, the load balancer's health check (commit 6) |
| `GET /centers` | All centres. Optional filters: `city` (`Mumbai`, `Hyderabad`), `pincode` (6 digits), `vaccine` (`COVISHIELD`, `COVAXIN`, `SPUTNIK V`), `center_type` (`GOVERNMENT`, `PRIVATE`) |
| `GET /centers/{id}` | One centre, e.g. `/centers/MUM-001`; `404` if unknown |
| `GET /docs` | Interactive API docs |

## Which server answered

Every response, including errors, says which server and zone served it:

```json
"served_by": {"instance_id": "i-0abc123", "availability_zone": "ap-south-1a",
              "region": "ap-south-1", "hostname": "ip-10-0-1-10", "source": "ec2-metadata"}
```

The same values are in the `X-Instance-Id` and `X-Availability-Zone` headers, so scripts
can count zones with `curl -I`. On EC2 they come from the instance metadata service
(IMDSv2), read once at startup. Off EC2 they fall back to `local`. Responses carry
`Cache-Control: no-store`, so during the failover test every request really reaches a server.

## Run locally

```bash
cd lookup
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
./scripts/smoke_test.sh                # in another terminal
```

To fake two zones on one laptop (handy before AWS is set up):

```bash
SERVED_BY_INSTANCE=demo-a SERVED_BY_AZ=ap-south-1a uvicorn app.main:app --port 8001 &
SERVED_BY_INSTANCE=demo-b SERVED_BY_AZ=ap-south-1b uvicorn app.main:app --port 8002 &
```

Tests (no database or AWS needed):

```bash
pip install -r requirements-dev.txt
pytest -v
```

The data file is `app/data/centers.json`. Centre names are fictional; the areas and PIN
codes are real places, used only for filtering. Not affiliated with the official CoWIN platform.
