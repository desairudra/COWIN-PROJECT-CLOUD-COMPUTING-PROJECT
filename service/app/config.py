"""Service settings, read from environment variables.

Every replica must get the same SECRET_KEY: OTP transactions and session
tokens are signed with it, so a request can land on any replica (stateless,
Sec 9.4). Nothing about a user is kept in process memory.
"""
import os
import secrets
import warnings
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    app_env: str = field(default_factory=lambda: os.environ.get("APP_ENV", "dev"))
    database_url: str = field(default_factory=lambda: os.environ.get(
        "DATABASE_URL", "postgresql://cowin:cowin@localhost:5432/cowin"))
    # Several databases, comma-separated, to split registrations by mobile number (commit 16).
    database_urls: list[str] = field(default_factory=lambda: _list("DATABASE_URLS"))
    db_pool_min: int = field(default_factory=lambda: int(os.environ.get("DB_POOL_MIN", "1")))
    db_pool_max: int = field(default_factory=lambda: int(os.environ.get("DB_POOL_MAX", "10")))

    secret_key: str = field(default_factory=lambda: os.environ.get("SECRET_KEY", ""))
    otp_length: int = field(default_factory=lambda: int(os.environ.get("OTP_LENGTH", "6")))
    otp_ttl_seconds: int = field(default_factory=lambda: int(os.environ.get("OTP_TTL_SECONDS", "180")))
    session_ttl_seconds: int = field(default_factory=lambda: int(os.environ.get("SESSION_TTL_SECONDS", "900")))

    # There is no SMS gateway in this project. With OTP_DELIVERY=demo the OTP is
    # returned in the API response so the live demo can be used; with "log"
    # it is only written to the service log.
    otp_delivery: str = field(default_factory=lambda: os.environ.get("OTP_DELIVERY", "demo"))

    # Redis for rate limits and OTP state (commit 12). Empty = no limits (local dev only).
    redis_url: str = field(default_factory=lambda: os.environ.get("REDIS_URL", ""))
    rate_limit_enabled: bool = field(default_factory=lambda: _bool("RATE_LIMIT_ENABLED", True))
    rate_limit_capacity: int = field(default_factory=lambda: int(os.environ.get("RATE_LIMIT_CAPACITY", "100")))
    rate_limit_window_s: float = field(default_factory=lambda: float(os.environ.get("RATE_LIMIT_WINDOW_S", "300")))
    otp_cooldown_s: int = field(default_factory=lambda: int(os.environ.get("OTP_COOLDOWN_S", "30")))
    otp_max_per_hour: int = field(default_factory=lambda: int(os.environ.get("OTP_MAX_PER_HOUR", "5")))
    otp_max_attempts: int = field(default_factory=lambda: int(os.environ.get("OTP_MAX_ATTEMPTS", "3")))
    # Proxies in front of the service whose X-Forwarded-For entry is trusted: 1 behind the ALB.
    trust_proxy: int = field(default_factory=lambda: int(os.environ.get("TRUST_PROXY", "0")))

    cors_origins: list[str] = field(default_factory=lambda: _list("CORS_ORIGINS"))
    # Directory with the web frontend; served at "/" if it exists.
    web_dir: str = field(default_factory=lambda: os.environ.get(
        "WEB_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "web")))

    def __post_init__(self):
        if not self.secret_key:
            if self.app_env != "dev":
                raise RuntimeError("SECRET_KEY must be set outside APP_ENV=dev "
                                   "(and must be the same on every replica)")
            warnings.warn("SECRET_KEY not set: using a random key. Fine for one local "
                          "process, wrong for more than one replica.")
            object.__setattr__(self, "secret_key", secrets.token_urlsafe(32))
        if len(self.secret_key) < 16:
            raise RuntimeError("SECRET_KEY must be at least 16 characters")
        if self.otp_delivery not in {"demo", "log"}:
            raise RuntimeError("OTP_DELIVERY must be 'demo' or 'log'")
        if not self.redis_url:
            if self.app_env not in {"dev", "test"}:
                raise RuntimeError("REDIS_URL must be set outside dev: it holds rate limits and OTP state")
            warnings.warn("REDIS_URL not set: no rate limits, OTPs reusable until they expire. Dev only.")
        if not 4 <= self.otp_length <= 8:
            raise RuntimeError("OTP_LENGTH must be between 4 and 8")
