"""Tests for the Vaccine Center Lookup microservice (commit 3).

    cd lookup && pip install -r requirements-dev.txt && pytest -v

No database or AWS account needed.
"""
import os
import pathlib
import re
import sys
from unittest import mock

os.environ["DISABLE_IMDS"] = "1"           # never probe 169.254.169.254 from CI
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest                                # noqa: E402
from fastapi.testclient import TestClient    # noqa: E402

from app import instance                     # noqa: E402
from app.main import create_app, load_centers  # noqa: E402

CENTERS = load_centers()


@pytest.fixture
def client():
    return TestClient(create_app())


@pytest.fixture
def ec2_client():
    """Pretend to be the server in ap-south-1a."""
    served = {"instance_id": "i-0abc123", "availability_zone": "ap-south-1a",
              "region": "ap-south-1", "hostname": "ip-10-0-1-10", "source": "ec2-metadata"}
    return TestClient(create_app(served_by=served))


# -- the static data ---------------------------------------------------------------

def test_data_has_twenty_centres_split_across_two_cities():
    assert len(CENTERS) == 20
    assert sum(c["city"] == "Mumbai" for c in CENTERS) == 10
    assert sum(c["city"] == "Hyderabad" for c in CENTERS) == 10


def test_data_ids_are_unique_and_well_formed():
    ids = [c["id"] for c in CENTERS]
    assert len(set(ids)) == len(ids)
    assert all(re.fullmatch(r"(MUM|HYD)-\d{3}", i) for i in ids)


def test_data_pincodes_match_their_city():
    for c in CENTERS:
        assert re.fullmatch(r"[1-9]\d{5}", c["pincode"]), c
        assert c["pincode"].startswith("400" if c["city"] == "Mumbai" else "500"), c
        assert c["state"] == ("Maharashtra" if c["city"] == "Mumbai" else "Telangana")


def test_data_every_centre_offers_a_vaccine_and_free_matches_type():
    for c in CENTERS:
        assert c["vaccines"], c
        assert c["free"] is (c["center_type"] == "GOVERNMENT")


# -- endpoints -------------------------------------------------------------------------

def test_info_lists_endpoints(client):
    body = client.get("/").json()
    assert body["service"] == "vaccine-center-lookup"
    assert "/centers" in body["endpoints"]


def test_health_is_ok_and_reports_the_server(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["centers_loaded"] == 20
    assert body["served_by"]["availability_zone"] == "local"


def test_list_all_centres(client):
    body = client.get("/centers").json()
    assert body["count"] == 20 and len(body["centers"]) == 20


@pytest.mark.parametrize("city, expected", [("Mumbai", 10), ("Hyderabad", 10)])
def test_filter_by_city(client, city, expected):
    body = client.get("/centers", params={"city": city}).json()
    assert body["count"] == expected
    assert {c["city"] for c in body["centers"]} == {city}


def test_filter_by_pincode(client):
    body = client.get("/centers", params={"pincode": "400058"}).json()
    assert [c["id"] for c in body["centers"]] == ["MUM-001"]


def test_pincode_with_no_centre_returns_empty_list(client):
    body = client.get("/centers", params={"pincode": "110001"}).json()
    assert body == {**body, "count": 0, "centers": []}


def test_filters_combine(client):
    body = client.get("/centers", params={"city": "Hyderabad", "vaccine": "COVAXIN",
                                          "center_type": "GOVERNMENT"}).json()
    assert body["count"] > 0
    for c in body["centers"]:
        assert c["city"] == "Hyderabad" and "COVAXIN" in c["vaccines"]
        assert c["center_type"] == "GOVERNMENT"


@pytest.mark.parametrize("params", [{"pincode": "4000"}, {"pincode": "040005"},
                                    {"pincode": "40005A"}, {"city": "Delhi"},
                                    {"vaccine": "PFIZER"}])
def test_bad_filters_are_rejected(client, params):
    assert client.get("/centers", params=params).status_code == 422


def test_get_one_centre_case_insensitive(client):
    body = client.get("/centers/hyd-003").json()
    assert body["center"]["area"] == "Secunderabad"


def test_unknown_centre_is_404_json(client):
    r = client.get("/centers/MUM-999")
    assert r.status_code == 404
    assert "MUM-999" in r.json()["error"] and "served_by" in r.json()


def test_unknown_path_is_404_json(client):
    r = client.get("/nope")
    assert r.status_code == 404 and "error" in r.json()


# -- proving which server answered (used by commits 6 and 8) ---------------------------

def test_every_response_names_the_server_and_zone(ec2_client):
    for path in ["/", "/health", "/centers", "/centers/MUM-001", "/centers/NOPE"]:
        r = ec2_client.get(path)
        assert r.headers["X-Availability-Zone"] == "ap-south-1a", path
        assert r.headers["X-Instance-Id"] == "i-0abc123", path
        assert r.json()["served_by"]["availability_zone"] == "ap-south-1a", path


def test_responses_are_never_cached(client):
    assert client.get("/centers").headers["Cache-Control"] == "no-store"


# -- instance detection ---------------------------------------------------------------

def test_detect_falls_back_to_local_when_metadata_is_unreachable(monkeypatch):
    monkeypatch.delenv("DISABLE_IMDS", raising=False)
    with mock.patch.object(instance, "_imds_token", side_effect=OSError("no route")):
        info = instance.detect()
    assert info["instance_id"] == "local" and info["source"] == "fallback"


def test_detect_reads_ec2_metadata(monkeypatch):
    monkeypatch.delenv("DISABLE_IMDS", raising=False)
    values = {"instance-id": "i-0def456", "placement/availability-zone": "ap-south-1b",
              "placement/region": "ap-south-1"}
    with mock.patch.object(instance, "_imds_token", return_value="tok"), \
         mock.patch.object(instance, "_imds_get", side_effect=lambda p, t: values[p]):
        info = instance.detect()
    assert info["availability_zone"] == "ap-south-1b"
    assert info["instance_id"] == "i-0def456" and info["source"] == "ec2-metadata"


def test_detect_env_override(monkeypatch):
    monkeypatch.setenv("SERVED_BY_INSTANCE", "demo-a")
    monkeypatch.setenv("SERVED_BY_AZ", "ap-south-1a")
    info = instance.detect()
    assert info == {**info, "instance_id": "demo-a", "availability_zone": "ap-south-1a",
                    "region": "ap-south-1", "source": "env"}


def test_monitor_page_is_served(client):
    r = client.get("/monitor")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Zone monitor" in r.text and "X-Availability-Zone" in r.text
    assert r.headers["Cache-Control"] == "no-store"


def test_info_lists_monitor(client):
    assert "/monitor" in client.get("/").json()["endpoints"]


def test_release_header_defaults_to_v1(client):
    r = client.get("/health")
    assert r.headers["X-App-Release"] == "v1"
    assert client.get("/").json()["release"] == "v1"


def test_detect_zone_only_uses_hostname(monkeypatch):
    monkeypatch.delenv("SERVED_BY_INSTANCE", raising=False)
    monkeypatch.setenv("SERVED_BY_AZ", "compose")
    info = instance.detect()
    assert info["instance_id"] == info["hostname"] and info["availability_zone"] == "compose"
