"""Vaccine Center Lookup microservice .

A deliberately small, stateless service that returns static JSON. Every
response says which server and Availability Zone answered (`served_by`, plus
X-Instance-Id / X-Availability-Zone headers), so the load balancer's spreading
 and the Zone A failover  can be seen and measured.

    GET /                     service info
    GET /health               health check for the load balancer
    GET /centers              all centres; filters: city, pincode, vaccine, center_type
    GET /centers/{center_id}  one centre
    GET /monitor              live page showing which zone answers each request

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000      (from lookup/)
Docs: http://localhost:8000/docs

Compatible with Python 3.9 (the default on Amazon Linux 2023), so no
`X | None` type syntax here.
"""
import json
import os
import pathlib
import time
from enum import Enum
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .instance import detect

DATA_FILE = pathlib.Path(__file__).parent / "data" / "centers.json"
MONITOR_PAGE = pathlib.Path(__file__).parent / "static" / "monitor.html"
VERSION = "1.0.0"
RELEASE = os.environ.get("APP_RELEASE", "v1")


class City(str, Enum):
    mumbai = "Mumbai"
    hyderabad = "Hyderabad"


class Vaccine(str, Enum):
    covishield = "COVISHIELD"
    covaxin = "COVAXIN"
    sputnik = "SPUTNIK V"


class CenterType(str, Enum):
    government = "GOVERNMENT"
    private = "PRIVATE"


def load_centers(path: pathlib.Path = DATA_FILE) -> List[Dict[str, Any]]:
    return json.loads(path.read_text())["centers"]


def create_app(served_by: Optional[Dict[str, str]] = None,
               centers: Optional[List[Dict[str, Any]]] = None) -> FastAPI:
    served_by = served_by or detect()
    centers = centers if centers is not None else load_centers()
    by_id = {c["id"]: c for c in centers}
    started = time.time()

    app = FastAPI(title="Vaccine Center Lookup (IT457 course project)", version=VERSION,
                  description="Static, made-up vaccination centres in Mumbai and Hyderabad. "
                              "Not affiliated with the official CoWIN platform.")

    @app.middleware("http")
    async def tag_response(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Instance-Id"] = served_by["instance_id"]
        response.headers["X-Availability-Zone"] = served_by["availability_zone"]
        response.headers["X-App-Release"] = RELEASE
        # Never let a browser or proxy cache an answer: during the failover
        # test every request must really reach a server.
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException):
        return JSONResponse({"error": exc.detail, "served_by": served_by}, exc.status_code)

    @app.get("/", tags=["info"])
    def info():
        return {
            "service": "vaccine-center-lookup",
            "version": VERSION,
            "release": RELEASE,
            "endpoints": ["/health", "/centers", "/centers/{center_id}", "/monitor", "/docs"],
            "served_by": served_by,
        }

    @app.get("/health", tags=["info"])
    def health():
        # Cheap on purpose: the ALB calls this every few seconds per server.
        return {"status": "ok", "centers_loaded": len(centers),
                "uptime_s": round(time.time() - started, 1), "served_by": served_by}

    @app.get("/monitor", include_in_schema=False)
    def monitor():
        return FileResponse(MONITOR_PAGE, media_type="text/html")

    @app.get("/centers", tags=["centers"])
    def list_centers(
        city: Optional[City] = Query(None, description="Mumbai or Hyderabad"),
        pincode: Optional[str] = Query(None, pattern=r"^[1-9][0-9]{5}$",
                                       description="6-digit Indian PIN code"),
        vaccine: Optional[Vaccine] = None,
        center_type: Optional[CenterType] = None,
    ):
        result = [
            c for c in centers
            if (city is None or c["city"] == city.value)
            and (pincode is None or c["pincode"] == pincode)
            and (vaccine is None or vaccine.value in c["vaccines"])
            and (center_type is None or c["center_type"] == center_type.value)
        ]
        return {"count": len(result), "centers": result, "served_by": served_by}

    @app.get("/centers/{center_id}", tags=["centers"])
    def get_center(center_id: str):
        center = by_id.get(center_id.upper())
        if center is None:
            raise HTTPException(404, f"No centre with id {center_id!r}")
        return {"center": center, "served_by": served_by}

    return app


app = create_app()
