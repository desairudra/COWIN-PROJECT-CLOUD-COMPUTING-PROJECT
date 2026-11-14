"""Request and response bodies. Pydantic rejects bad input with 422 before the
database is touched. The database still checks everything again (commit 1),
so the rules hold even if this layer has a bug."""
from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

MOBILE_PATTERN = r"^[6-9][0-9]{9}$"


class Gender(str, Enum):
    MALE = "MALE"
    FEMALE = "FEMALE"
    OTHER = "OTHER"


class PhotoIdType(str, Enum):
    AADHAAR = "AADHAAR"
    DRIVING_LICENSE = "DRIVING_LICENSE"
    PAN = "PAN"
    PASSPORT = "PASSPORT"
    PENSION_PASSBOOK = "PENSION_PASSBOOK"
    NPR_SMART_CARD = "NPR_SMART_CARD"
    VOTER_ID = "VOTER_ID"
    UDID = "UDID"
    RATION_CARD = "RATION_CARD"


class OtpRequest(BaseModel):
    mobile: str = Field(pattern=MOBILE_PATTERN, examples=["9876543210"])


class OtpRequested(BaseModel):
    txn_id: str
    expires_in: int
    # Only present when OTP_DELIVERY=demo (no SMS gateway in this project).
    demo_otp: str | None = None


class OtpVerify(BaseModel):
    txn_id: str = Field(min_length=10, max_length=1000)
    otp: str = Field(pattern=r"^[0-9]{4,8}$")


class Session(BaseModel):
    token: str
    token_type: str = "Bearer"
    expires_in: int
    mobile: str
    new_registration: bool
    beneficiary_count: int


class BeneficiaryIn(BaseModel):
    full_name: str = Field(min_length=1, max_length=100)
    birth_year: int = Field(ge=1900, le=2100)
    gender: Gender
    photo_id_type: PhotoIdType
    photo_id_number: str = Field(min_length=4, max_length=40)

    @field_validator("full_name")
    @classmethod
    def name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("full_name must not be blank")
        return v

    @field_validator("birth_year")
    @classmethod
    def not_in_future(cls, v: int) -> int:
        if v > datetime.now().year:
            raise ValueError("birth_year cannot be in the future")
        return v


class BeneficiaryCreated(BaseModel):
    beneficiary_id: UUID
    created: bool          # false = this was a replayed retry
    created_at: datetime


class Beneficiary(BaseModel):
    beneficiary_id: UUID
    full_name: str
    birth_year: int
    gender: Gender
    photo_id_type: PhotoIdType
    photo_id_masked: str
    created_at: datetime


class BeneficiaryList(BaseModel):
    mobile: str
    count: int
    max_allowed: int = 4
    beneficiaries: list[Beneficiary]
