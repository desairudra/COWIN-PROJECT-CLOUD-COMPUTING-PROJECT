"""One JSON error shape for every failure:  {"error": {"code": ..., "message": ...}}

Maps the database's SQLSTATE codes (catalogue in db/README.md) to HTTP.
"""
import logging

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("cowin.errors")

SQLSTATE_TO_HTTP = {
    "CW001": (409, "BENEFICIARY_LIMIT", "This mobile number already has the maximum of 4 beneficiaries."),
    "CW002": (401, "NOT_REGISTERED", "This mobile number is not registered. Verify the OTP again."),
    "CW003": (409, "PHOTO_ID_IN_USE", "This photo ID is already registered."),
    "CW004": (422, "IDEMPOTENCY_KEY_REUSED", "This Idempotency-Key was already used for a different request."),
    "CW005": (400, "IMMUTABLE_FIELD", "That field cannot be changed."),
    "23514": (422, "INVALID_INPUT", "One of the fields is not valid."),
    "22P02": (422, "INVALID_INPUT", "One of the fields is not valid."),
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict | None = None):
        self.status, self.code, self.message, self.headers = status, code, message, headers


def body(code: str, message: str, details=None) -> dict:
    err = {"code": code, "message": message}
    if details is not None:
        err["details"] = details
    return {"error": err}


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api(_: Request, exc: ApiError):
        return JSONResponse(body(exc.code, exc.message), exc.status, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        details = [{"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]}
                   for e in exc.errors()]
        return JSONResponse(body("INVALID_INPUT", "Some fields are not valid.", details), 422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        return JSONResponse(body(f"HTTP_{exc.status_code}", str(exc.detail)), exc.status_code)

    @app.exception_handler(psycopg.Error)
    async def _db(_: Request, exc: psycopg.Error):
        if exc.sqlstate in SQLSTATE_TO_HTTP:
            status, code, message = SQLSTATE_TO_HTTP[exc.sqlstate]
            return JSONResponse(body(code, message), status)
        if isinstance(exc, psycopg.OperationalError):
            log.error("database unavailable: %s", exc)
            return JSONResponse(body("DB_UNAVAILABLE", "Database unavailable, please retry."),
                                503, headers={"Retry-After": "1"})
        log.exception("unexpected database error")
        return JSONResponse(body("INTERNAL", "Unexpected error."), 500)
