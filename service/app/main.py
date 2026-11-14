"""CoWIN registration service (commit 11). Report Sec 9.2 and 9.4.

Four operations:
    POST /api/v1/otp/request      request an OTP for a mobile number
    POST /api/v1/otp/verify       verify it, get a session token (registers the mobile)
    POST /api/v1/beneficiaries    add a beneficiary   (needs Idempotency-Key header)
    GET  /api/v1/beneficiaries    list beneficiaries under the mobile

Stateless: no user data is kept in process memory. OTP transactions and
sessions are signed tokens, and everything durable is in PostgreSQL. Any
replica can serve any request, which is what the load balancer and the
autoscaler (commit 14) need.

Retry-safe: verify-OTP is an upsert and add-beneficiary is keyed by the
client's Idempotency-Key, so repeating any request never creates duplicates.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000   (from service/)
Docs: http://localhost:8000/docs
"""
import logging
import os
import re
import socket
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import errors
from .config import Settings
from .db import Database
from .errors import ApiError
from .schemas import (BeneficiaryCreated, BeneficiaryIn, BeneficiaryList, OtpRequest,
                      OtpRequested, OtpVerify, Session)
from .security import Signer, TokenError

log = logging.getLogger("cowin.service")
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_\-]{8,64}$")
HOSTNAME = socket.gethostname()


def mask(mobile: str) -> str:
    return "XXXXXX" + mobile[-4:]


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    settings = settings or Settings()
    db = database or Database(settings.database_url, settings.db_pool_min, settings.db_pool_max)
    signer = Signer(settings.secret_key)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        db.open()
        yield
        db.close()

    app = FastAPI(title="CoWIN Registration Service (IT457 course project)",
                  version="0.2.0", lifespan=lifespan,
                  description="Scaled-down model of CoWIN's registration module. "
                              "Not affiliated with the official CoWIN platform.")
    errors.install(app)
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                           allow_methods=["GET", "POST"],
                           allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
                           expose_headers=["Idempotent-Replayed", "X-Served-By"])

    @app.middleware("http")
    async def served_by(request: Request, call_next):
        # Which replica answered. Used in E2/E3 to see the load balancer spread requests.
        response = await call_next(request)
        response.headers["X-Served-By"] = HOSTNAME
        return response

    def current_mobile(authorization: str | None = Header(default=None)) -> str:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise ApiError(401, "UNAUTHENTICATED", "Missing bearer token.",
                           {"WWW-Authenticate": "Bearer"})
        try:
            return signer.session_mobile(authorization[7:].strip())
        except TokenError:
            raise ApiError(401, "UNAUTHENTICATED", "Session is invalid or has expired. Log in again.",
                           {"WWW-Authenticate": "Bearer"})

    # -- health (for Kubernetes probes, commit 14) ---------------------------
    @app.get("/healthz", tags=["health"])
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    def readyz():
        db.ping()
        return {"status": "ready"}

    # -- 1. request OTP -------------------------------------------------------
    @app.post("/api/v1/otp/request", response_model=OtpRequested,
              response_model_exclude_none=True, tags=["auth"])
    def request_otp(req: OtpRequest):
        txn_id, otp = signer.new_otp_transaction(req.mobile, settings.otp_length,
                                                 settings.otp_ttl_seconds)
        # No SMS gateway in this project. In production this would be put on a
        # queue (SQS/SNS in the Sec 9.5 design) so sending never blocks the request.
        log.info("OTP for %s: %s", mask(req.mobile), otp)
        return OtpRequested(txn_id=txn_id, expires_in=settings.otp_ttl_seconds,
                            demo_otp=otp if settings.otp_delivery == "demo" else None)

    # -- 2. verify OTP --------------------------------------------------------
    @app.post("/api/v1/otp/verify", response_model=Session, tags=["auth"])
    def verify_otp(req: OtpVerify):
        try:
            mobile = signer.check_otp(req.txn_id, req.otp)
        except TokenError as exc:
            if str(exc) == "token expired":
                raise ApiError(401, "OTP_EXPIRED", "OTP has expired. Request a new one.")
            raise ApiError(401, "OTP_INVALID", "OTP is incorrect.")
        reg = db.upsert_registration(mobile)          # retry-safe upsert
        return Session(token=signer.new_session(mobile, settings.session_ttl_seconds),
                       expires_in=settings.session_ttl_seconds, mobile=mobile,
                       new_registration=reg["created"],
                       beneficiary_count=reg["beneficiary_count"])

    # -- 3. add beneficiary ---------------------------------------------------
    @app.post("/api/v1/beneficiaries", response_model=BeneficiaryCreated,
              status_code=201, tags=["beneficiaries"])
    def add_beneficiary(body: BeneficiaryIn, response: Response,
                        mobile: str = Depends(current_mobile),
                        idempotency_key: str | None = Header(default=None)):
        if not idempotency_key:
            raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED",
                           "Send an Idempotency-Key header (e.g. a UUID), "
                           "and reuse it if you retry the same request.")
        if not IDEMPOTENCY_KEY.match(idempotency_key):
            raise ApiError(400, "IDEMPOTENCY_KEY_INVALID",
                           "Idempotency-Key must be 8-64 characters of A-Z, a-z, 0-9, '_' or '-'.")
        row = db.add_beneficiary(mobile, idempotency_key, body)
        if not row["created"]:
            response.status_code = 200
            response.headers["Idempotent-Replayed"] = "true"
        return BeneficiaryCreated(**row)

    # -- 4. list beneficiaries ------------------------------------------------
    @app.get("/api/v1/beneficiaries", response_model=BeneficiaryList, tags=["beneficiaries"])
    def list_beneficiaries(mobile: str = Depends(current_mobile)):
        rows = db.list_beneficiaries(mobile)
        return BeneficiaryList(mobile=mobile, count=len(rows), beneficiaries=rows)

    # -- web frontend (if present) --------------------------------------------
    web_dir = os.path.abspath(settings.web_dir)
    if os.path.isfile(os.path.join(web_dir, "index.html")):
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")

    return app


logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")


def __getattr__(name):
    # `uvicorn app.main:app` builds the app on first access, so importing this
    # module in tests does not need a database or environment variables.
    if name == "app":
        global app
        app = create_app()
        return app
    raise AttributeError(name)
