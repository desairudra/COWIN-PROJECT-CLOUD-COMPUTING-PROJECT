"""Which server answered: instance ID and Availability Zone.

On EC2 these come from the instance metadata service (IMDSv2: get a session
token with PUT, then read the values with it). This is how the failover test
(commit 8) proves which zone served each request.

Off EC2 (laptop, CI), the metadata service does not answer, so we fall back to
"local". The values are read once at startup and cached: the health check is
called every few seconds and must stay fast.

Overrides, useful for local demos and tests:
    SERVED_BY_INSTANCE=i-demo-a SERVED_BY_AZ=ap-south-1a uvicorn app.main:app
    DISABLE_IMDS=1   skip the metadata lookup entirely
"""
import os
import socket
import urllib.request
from typing import Dict, Optional

IMDS = "http://169.254.169.254/latest"
TIMEOUT_S = 0.5


def _imds_token() -> Optional[str]:
    req = urllib.request.Request(
        f"{IMDS}/api/token", method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "300"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return resp.read().decode()


def _imds_get(path: str, token: str) -> str:
    req = urllib.request.Request(f"{IMDS}/meta-data/{path}",
                                 headers={"X-aws-ec2-metadata-token": token})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return resp.read().decode().strip()


def detect() -> Dict[str, str]:
    """Returns {"instance_id", "availability_zone", "region", "hostname", "source"}."""
    info = {
        "instance_id": "local",
        "availability_zone": "local",
        "region": "local",
        "hostname": socket.gethostname(),
        "source": "fallback",
    }
    if os.environ.get("SERVED_BY_INSTANCE") or os.environ.get("SERVED_BY_AZ"):
        info["instance_id"] = os.environ.get("SERVED_BY_INSTANCE", "local")
        info["availability_zone"] = os.environ.get("SERVED_BY_AZ", "local")
        info["region"] = info["availability_zone"][:-1] if info["availability_zone"] != "local" else "local"
        info["source"] = "env"
        return info
    if os.environ.get("DISABLE_IMDS"):
        return info
    try:
        token = _imds_token()
        info["instance_id"] = _imds_get("instance-id", token)
        info["availability_zone"] = _imds_get("placement/availability-zone", token)
        info["region"] = _imds_get("placement/region", token)
        info["source"] = "ec2-metadata"
    except Exception:
        pass          # not on EC2, or metadata unreachable: keep "local"
    return info
