from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# auth
class OtpRequestIn(BaseModel):
    phone: str = Field(examples=["9876543210"])


class OtpRequestOut(BaseModel):
    request_id: str
    expires_in: int
    demo_otp: str | None = None


class OtpVerifyIn(BaseModel):
    request_id: str
    otp: str


class UserOut(ORM):
    id: int
    phone: str
    name: str | None
    email: str | None
    role: str
    kyc_status: str
    pdo_status: str
    pdo_business_name: str | None
    created_at: datetime


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class ProfileIn(BaseModel):
    name: str | None = Field(None, max_length=120)
    email: str | None = Field(None, max_length=200)


class KycIn(BaseModel):
    doc_type: str = Field(examples=["aadhaar", "pan", "voter_id", "driving_license"])
    doc_number: str = Field(min_length=4, max_length=30)
    name_on_doc: str = Field(max_length=120)


class PdoApplicationIn(BaseModel):
    business_name: str = Field(max_length=200)
    address: str


# plans / subscriptions
class PlanOut(ORM):
    id: int
    name: str
    description: str | None
    price_paise: int
    validity_minutes: int
    data_limit_mb: int | None
    speed_down_kbps: int | None
    speed_up_kbps: int | None
    is_active: bool


class PlanIn(BaseModel):
    name: str
    description: str | None = None
    price_paise: int = Field(ge=0)
    validity_minutes: int = Field(gt=0)
    data_limit_mb: int | None = Field(None, gt=0)
    speed_down_kbps: int | None = Field(None, gt=0)
    speed_up_kbps: int | None = Field(None, gt=0)
    daily_claim_limit: int | None = Field(None, gt=0)
    is_active: bool = True
    sort_order: int = 0


class SubscriptionOut(ORM):
    id: int
    plan: PlanOut
    starts_at: datetime
    expires_at: datetime
    data_limit_bytes: int | None
    source: str
    status: str


class BuyPlanIn(BaseModel):
    method: str = Field("wallet", examples=["wallet", "gateway", "free"])


# wallet / payments
class WalletTxOut(ORM):
    id: int
    amount_paise: int
    balance_after_paise: int
    kind: str
    note: str | None
    created_at: datetime


class WalletOut(BaseModel):
    balance_paise: int
    transactions: list[WalletTxOut]


class TopupIn(BaseModel):
    amount_paise: int = Field(ge=100, le=1_000_000)


class OrderOut(BaseModel):
    order_id: int
    gateway: str                      # razorpay | mock
    gateway_order_id: str
    amount_paise: int
    currency: str = "INR"
    razorpay_key_id: str | None
    status: str


class PaymentVerifyIn(BaseModel):
    order_id: int
    razorpay_payment_id: str
    razorpay_signature: str


# hotspots / wifi
class HotspotOut(BaseModel):
    id: int
    name: str
    ssid: str
    nas_id: str
    lat: float | None
    lng: float | None
    address: str | None
    distance_km: float | None
    online: bool
    active_sessions: int


class WifiLoginIn(BaseModel):
    """Values come from the hotspot's redirect URL.
    CoovaChilli: http://<portal>/chilli?res=notyet&uamip=...&uamport=...&challenge=...&mac=...
    MikroTik:    http://<portal>/mikrotik?link_login=...&chap_id=...&chap_challenge=...&mac=...&nasid=..."""
    mac: str
    nasid: str | None = None
    # CoovaChilli
    uamip: str | None = Field(None, examples=["10.1.0.1"])
    uamport: str | None = Field(None, examples=["3990"])
    challenge: str | None = None
    userurl: str | None = None
    # MikroTik Hotspot
    link_login: str | None = Field(None, examples=["http://10.5.50.1/login"])
    chap_id: str | None = None
    chap_challenge: str | None = None
    dst: str | None = None


class WifiLoginOut(BaseModel):
    logon_url: str
    session_timeout: int
    remaining_bytes: int | None
    plan: PlanOut


class WifiStatusOut(BaseModel):
    subscription: SubscriptionOut | None
    remaining_bytes: int | None
    seconds_left: int | None
    online_now: bool
    current_session: dict | None


class AccessPointIn(BaseModel):
    name: str = Field(max_length=120)
    ssid: str = Field("PMWANI-Free", max_length=64)
    mac: str | None = None
    lat: float | None = Field(None, ge=-90, le=90)
    lng: float | None = Field(None, ge=-180, le=180)
    address: str | None = None


class AccessPointOut(ORM):
    id: int
    pdo_id: int | None
    name: str
    nas_id: str
    ssid: str
    mac: str | None
    lat: float | None
    lng: float | None
    address: str | None
    status: str
    created_at: datetime


class StatusIn(BaseModel):
    status: str


class DecisionIn(BaseModel):
    approve: bool
