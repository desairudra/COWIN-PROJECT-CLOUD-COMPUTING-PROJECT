"""Stateless OTP transactions and session tokens.

The flow mirrors CoWIN's public API: "generate OTP" returns a txnId, and
"confirm OTP" takes the OTP plus that txnId and returns a bearer token.

To keep the service stateless (Sec 9.4) the txnId is not a database key. It is
a signed token that carries the mobile number, an expiry, and an HMAC of the
OTP keyed with the server secret. Any replica with the same SECRET_KEY can
check it, and nobody can brute-force the OTP offline from the txnId because
they do not have the key.

Known limits, fixed by commit 12 (Redis): a txnId can be tried until it
expires, and there is no per-mobile cap on OTP requests or wrong attempts.
Redis adds one-time use, attempt counters and the rate limiter.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time


class TokenError(Exception):
    """Token is malformed, has a bad signature, has the wrong type, or has expired."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Signer:
    def __init__(self, secret_key: str):
        self._key = hashlib.sha256(("cowin-token|" + secret_key).encode()).digest()
        self._otp_key = hashlib.sha256(("cowin-otp|" + secret_key).encode()).digest()

    # -- generic signed token: base64url(json).base64url(hmac) ---------------
    def sign(self, payload: dict) -> str:
        body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        sig = _b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest())
        return f"{body}.{sig}"

    def verify(self, token: str, expected_type: str) -> dict:
        try:
            body, sig = token.split(".")
            good = _b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(sig, good):
                raise TokenError("bad signature")
            payload = json.loads(_unb64(body))
        except TokenError:
            raise
        except Exception as exc:          # malformed base64 / json / shape
            raise TokenError("malformed token") from exc
        if payload.get("typ") != expected_type:
            raise TokenError("wrong token type")
        if int(payload.get("exp", 0)) < int(time.time()):
            raise TokenError("token expired")
        return payload

    # -- OTP ------------------------------------------------------------------
    def otp_mac(self, nonce: str, mobile: str, otp: str) -> str:
        msg = f"{nonce}|{mobile}|{otp}".encode()
        return _b64(hmac.new(self._otp_key, msg, hashlib.sha256).digest())

    def new_otp_transaction(self, mobile: str, length: int, ttl: int) -> tuple[str, str]:
        otp = "".join(secrets.choice("0123456789") for _ in range(length))
        nonce = secrets.token_urlsafe(12)
        txn_id = self.sign({
            "typ": "otp", "sub": mobile, "n": nonce,
            "m": self.otp_mac(nonce, mobile, otp),
            "exp": int(time.time()) + ttl,
        })
        return txn_id, otp

    def check_otp(self, txn_id: str, otp: str) -> str:
        """Returns the mobile number if the OTP matches the transaction."""
        payload = self.verify(txn_id, "otp")
        good = self.otp_mac(payload["n"], payload["sub"], otp)
        if not hmac.compare_digest(payload["m"], good):
            raise TokenError("wrong OTP")
        return payload["sub"]

    # -- session --------------------------------------------------------------
    def new_session(self, mobile: str, ttl: int) -> str:
        return self.sign({"typ": "session", "sub": mobile, "exp": int(time.time()) + ttl})

    def session_mobile(self, token: str) -> str:
        return self.verify(token, "session")["sub"]
