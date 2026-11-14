"""Rate limits and OTP state in Redis (commit 12), shared by every replica.

Per client:  token bucket, RATE_LIMIT_CAPACITY calls per RATE_LIMIT_WINDOW_S (100 per 300 s).
Per mobile:  one OTP per OTP_COOLDOWN_S, at most OTP_MAX_PER_HOUR per hour.
Per OTP:     one-time use; locked after OTP_MAX_ATTEMPTS wrong guesses.

If Redis is down, rate limits fail open (the site stays usable) and OTP checks
fail closed (a request is refused rather than checked without limits).
"""
import logging
import math
from dataclasses import dataclass

import redis

log = logging.getLogger("cowin.limits")

TOKEN_BUCKET = """
local cap, rate = tonumber(ARGV[1]), tonumber(ARGV[2])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local b = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(b[1]) or cap
local ts = tonumber(b[2]) or now
tokens = math.min(cap, tokens + math.max(0, now - ts) * rate)
local allowed = 0
if tokens >= 1 then tokens = tokens - 1; allowed = 1 end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('EXPIRE', KEYS[1], math.ceil(cap / rate) + 1)
local retry = 0
if allowed == 0 then retry = (1 - tokens) / rate end
return {allowed, tostring(tokens), tostring(retry)}
"""


@dataclass
class Decision:
    allowed: bool
    remaining: int
    retry_after: int


class LimitsUnavailable(Exception):
    pass


class Limits:
    def __init__(self, url: str, settings):
        self.s = settings
        self.r = redis.Redis.from_url(url, socket_timeout=0.5, socket_connect_timeout=0.5,
                                      decode_responses=True)
        self._bucket = self.r.register_script(TOKEN_BUCKET)

    def ping(self) -> bool:
        return bool(self.r.ping())

    def take(self, key: str, capacity: int, window_s: float) -> Decision:
        allowed, tokens, retry = self._bucket(keys=[key], args=[capacity, capacity / window_s])
        return Decision(bool(allowed), int(float(tokens)), math.ceil(float(retry)))

    def client(self, client_id: str) -> Decision:
        try:
            return self.take(f"rl:client:{client_id}", self.s.rate_limit_capacity, self.s.rate_limit_window_s)
        except redis.RedisError as exc:
            log.warning("rate limiter unavailable, allowing request: %s", exc)
            return Decision(True, -1, 0)

    def otp_request(self, mobile: str) -> Decision:
        try:
            if self.s.otp_cooldown_s > 0:
                if not self.r.set(f"otp:cool:{mobile}", 1, nx=True, ex=self.s.otp_cooldown_s):
                    return Decision(False, 0, max(1, self.r.ttl(f"otp:cool:{mobile}")))
            return self.take(f"rl:otp:{mobile}", self.s.otp_max_per_hour, 3600)
        except redis.RedisError as exc:
            raise LimitsUnavailable(str(exc))

    def open_txn(self, nonce: str, mobile: str, ttl: int) -> None:
        try:
            self.r.set(f"otp:txn:{nonce}", mobile, ex=ttl)
        except redis.RedisError as exc:
            raise LimitsUnavailable(str(exc))

    def attempt(self, nonce: str, ttl: int) -> tuple:
        """Returns (txn_still_open, attempt_number)."""
        try:
            pipe = self.r.pipeline()
            pipe.exists(f"otp:txn:{nonce}")
            pipe.incr(f"otp:tries:{nonce}")
            pipe.expire(f"otp:tries:{nonce}", ttl)
            open_, tries, _ = pipe.execute()
            return bool(open_), int(tries)
        except redis.RedisError as exc:
            raise LimitsUnavailable(str(exc))

    def close_txn(self, nonce: str) -> bool:
        """True if this call used up the OTP (so two concurrent verifies cannot both win)."""
        try:
            return self.r.delete(f"otp:txn:{nonce}") == 1
        except redis.RedisError as exc:
            raise LimitsUnavailable(str(exc))


def client_id(request, trusted_proxies: int) -> str:
    """The caller's IP. Behind the ALB (TRUST_PROXY=1) it is the last X-Forwarded-For entry,
    the one the load balancer added; earlier entries are whatever the client sent."""
    xff = request.headers.get("x-forwarded-for", "")
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    if trusted_proxies > 0 and len(hops) >= trusted_proxies:
        return hops[-trusted_proxies]
    return request.client.host if request.client else "unknown"
