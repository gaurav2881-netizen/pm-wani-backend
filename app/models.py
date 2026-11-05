"""Application tables. FreeRADIUS tables (radcheck, radreply, radacct...) live in the same
database but are created from FreeRADIUS's own schema.sql and accessed with plain SQL."""
from datetime import datetime, timezone

from sqlalchemy import (BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "app_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(10), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(10), default="consumer")          # consumer | pdo | pdoa
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # KYC (store only what is needed; never the full document number)
    kyc_status: Mapped[str] = mapped_column(String(10), default="none")        # none | submitted | verified | rejected
    kyc_doc_type: Mapped[str | None] = mapped_column(String(20))
    kyc_doc_last4: Mapped[str | None] = mapped_column(String(4))
    # PDO onboarding
    pdo_status: Mapped[str] = mapped_column(String(10), default="none")        # none | pending | approved | rejected
    pdo_business_name: Mapped[str | None] = mapped_column(String(200))
    pdo_address: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class OtpRequest(Base):
    __tablename__ = "app_otp_requests"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    phone: Mapped[str] = mapped_column(String(10), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AccessPoint(Base):
    """A hotspot AP. nas_id must equal CoovaChilli's radiusnasid / radiuscalled on that AP."""
    __tablename__ = "app_access_points"

    id: Mapped[int] = mapped_column(primary_key=True)
    pdo_id: Mapped[int | None] = mapped_column(ForeignKey("app_users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    nas_id: Mapped[str] = mapped_column(String(64), unique=True)
    ssid: Mapped[str] = mapped_column(String(64), default="PMWANI-Free")
    mac: Mapped[str | None] = mapped_column(String(17))
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)
    address: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="pending")         # pending | active | suspended
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    pdo: Mapped["User | None"] = relationship()


class Plan(Base):
    __tablename__ = "app_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(Text)
    price_paise: Mapped[int] = mapped_column(Integer)                          # 0 = free plan
    validity_minutes: Mapped[int] = mapped_column(Integer)
    data_limit_mb: Mapped[int | None] = mapped_column(Integer)                 # None = unlimited
    speed_down_kbps: Mapped[int | None] = mapped_column(Integer)               # None = no speed limit
    speed_up_kbps: Mapped[int | None] = mapped_column(Integer)
    daily_claim_limit: Mapped[int | None] = mapped_column(Integer)             # free plans: claims per 24h
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Subscription(Base):
    """A plan a user owns. Purchases while a plan is running are queued after it."""
    __tablename__ = "app_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("app_plans.id"))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    data_limit_bytes: Mapped[int | None] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(10))                            # free | wallet | payment | admin
    status: Mapped[str] = mapped_column(String(10), default="active")          # active | exhausted | cancelled
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    plan: Mapped[Plan] = relationship()


class Order(Base):
    __tablename__ = "app_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id"), index=True)
    purpose: Mapped[str] = mapped_column(String(15))                           # wallet_topup | plan
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("app_plans.id"))
    amount_paise: Mapped[int] = mapped_column(Integer)
    gateway: Mapped[str] = mapped_column(String(10))                           # razorpay | mock
    gateway_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    gateway_payment_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(10), default="created")         # created | paid | failed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WalletTransaction(Base):
    __tablename__ = "app_wallet_transactions"
    __table_args__ = (UniqueConstraint("kind", "reference", name="uq_wallet_kind_ref"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id"), index=True)
    amount_paise: Mapped[int] = mapped_column(Integer)                         # + credit, - debit
    balance_after_paise: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(15))                              # topup | plan_purchase | refund | adjustment
    reference: Mapped[str] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WifiLogin(Base):
    """Audit log of every hotspot login we authorised (who, which AP, which device)."""
    __tablename__ = "app_wifi_logins"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id"), index=True)
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("app_subscriptions.id"))
    nas_id: Mapped[str | None] = mapped_column(String(64))
    client_mac: Mapped[str | None] = mapped_column(String(17))
    client_ip: Mapped[str | None] = mapped_column(String(45))
    channel: Mapped[str] = mapped_column(String(10))                           # app | portal
    user_agent: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
