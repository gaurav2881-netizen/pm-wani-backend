"""Wallet, subscriptions and Razorpay orders."""
import hashlib
import hmac
import secrets
from datetime import timedelta

import httpx
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import Order, Plan, Subscription, User, WalletTransaction, utcnow

MB = 1024 * 1024


# ------------------------------------------------------------------ wallet
def wallet_balance(db: Session, user_id: int) -> int:
    return int(db.scalar(select(func.coalesce(func.sum(WalletTransaction.amount_paise), 0))
                         .where(WalletTransaction.user_id == user_id)))


def wallet_post(db: Session, user: User, amount_paise: int, kind: str, reference: str, note: str | None = None):
    """Add a wallet entry (caller commits). Idempotent per (kind, reference)."""
    existing = db.scalar(select(WalletTransaction).where(
        WalletTransaction.kind == kind, WalletTransaction.reference == reference))
    if existing:
        return existing
    # lock the user row so two concurrent debits can't both pass the balance check
    db.execute(select(User.id).where(User.id == user.id).with_for_update())
    balance = wallet_balance(db, user.id) + amount_paise
    if balance < 0:
        raise HTTPException(402, "insufficient_balance")
    tx = WalletTransaction(user_id=user.id, amount_paise=amount_paise, balance_after_paise=balance,
                           kind=kind, reference=reference, note=note)
    db.add(tx)
    db.flush()
    return tx


# ----------------------------------------------------------- subscriptions
def usage_bytes(db: Session, username: str, since, until=None) -> int:
    from sqlalchemy import text
    q = ("SELECT COALESCE(SUM(COALESCE(acctinputoctets,0) + COALESCE(acctoutputoctets,0)),0) "
         "FROM radacct WHERE username = :u AND acctstarttime >= :since")
    params = {"u": username, "since": since}
    if until is not None:
        q += " AND acctstarttime < :until"
        params["until"] = until
    return int(db.execute(text(q), params).scalar())


def current_subscription(db: Session, user: User) -> tuple[Subscription | None, int | None]:
    """The running subscription and its remaining bytes (None = unlimited).
    Marks subscriptions whose data is used up as exhausted."""
    now = utcnow()
    subs = db.scalars(select(Subscription).where(
        Subscription.user_id == user.id, Subscription.status == "active",
        Subscription.starts_at <= now, Subscription.expires_at > now,
    ).order_by(Subscription.expires_at)).all()
    for sub in subs:
        if sub.data_limit_bytes is None:
            return sub, None
        used = usage_bytes(db, user.phone, sub.starts_at, sub.expires_at)
        remaining = sub.data_limit_bytes - used
        if remaining > 0:
            return sub, remaining
        sub.status = "exhausted"
        sub.expires_at = now
        db.flush()
        _rechain_queued(db, user, now)
        db.commit()
        return current_subscription(db, user)
    return None, None


def _rechain_queued(db: Session, user: User, now) -> None:
    """After a plan ends early (data used up), pull queued plans forward so the next one starts now."""
    queued = db.scalars(select(Subscription).where(
        Subscription.user_id == user.id, Subscription.status == "active", Subscription.starts_at > now,
    ).order_by(Subscription.starts_at)).all()
    running_end = db.scalar(select(func.max(Subscription.expires_at)).where(
        Subscription.user_id == user.id, Subscription.status == "active",
        Subscription.starts_at <= now, Subscription.expires_at > now))
    cursor = running_end or now
    for q in queued:
        duration = q.expires_at - q.starts_at
        q.starts_at, q.expires_at = cursor, cursor + duration
        cursor = q.expires_at


def grant_plan(db: Session, user: User, plan: Plan, source: str) -> Subscription:
    """Create a subscription (caller commits). Queued after the user's latest running plan."""
    now = utcnow()
    latest_end = db.scalar(select(func.max(Subscription.expires_at)).where(
        Subscription.user_id == user.id, Subscription.status == "active", Subscription.expires_at > now))
    start = max(now, latest_end) if latest_end else now
    sub = Subscription(
        user_id=user.id, plan_id=plan.id, starts_at=start,
        expires_at=start + timedelta(minutes=plan.validity_minutes),
        data_limit_bytes=plan.data_limit_mb * MB if plan.data_limit_mb else None,
        source=source,
    )
    db.add(sub)
    db.flush()
    return sub


def claim_free_plan(db: Session, user: User, plan: Plan) -> Subscription:
    if plan.price_paise != 0:
        raise HTTPException(400, "plan_not_free")
    limit = plan.daily_claim_limit or 1
    claims = db.scalar(select(func.count()).select_from(Subscription).where(
        Subscription.user_id == user.id, Subscription.plan_id == plan.id,
        Subscription.created_at > utcnow() - timedelta(hours=24)))
    if claims >= limit:
        raise HTTPException(429, "free_plan_already_claimed")
    sub = grant_plan(db, user, plan, "free")
    db.commit()
    return sub


def buy_with_wallet(db: Session, user: User, plan: Plan) -> Subscription:
    ref = secrets.token_hex(8)
    wallet_post(db, user, -plan.price_paise, "plan_purchase", ref, f"Plan: {plan.name}")
    sub = grant_plan(db, user, plan, "wallet")
    db.commit()
    return sub


# ------------------------------------------------------------------ orders
def create_order(db: Session, user: User, purpose: str, amount_paise: int, plan: Plan | None = None) -> Order:
    s = get_settings()
    if amount_paise < 100:
        raise HTTPException(400, "amount_too_small")
    receipt = f"u{user.id}-{secrets.token_hex(6)}"
    if s.payments_mock:
        gateway, gateway_order_id = "mock", f"mock_{secrets.token_hex(10)}"
    else:
        r = httpx.post("https://api.razorpay.com/v1/orders",
                       auth=(s.razorpay_key_id, s.razorpay_key_secret),
                       json={"amount": amount_paise, "currency": "INR", "receipt": receipt,
                             "notes": {"purpose": purpose, "user_id": str(user.id)}},
                       timeout=15)
        if r.status_code >= 400:
            raise HTTPException(502, "payment_gateway_error")
        gateway, gateway_order_id = "razorpay", r.json()["id"]
    order = Order(user_id=user.id, purpose=purpose, plan_id=plan.id if plan else None,
                  amount_paise=amount_paise, gateway=gateway, gateway_order_id=gateway_order_id)
    db.add(order)
    db.commit()
    return order


def checkout_signature_ok(order: Order, payment_id: str, signature: str) -> bool:
    s = get_settings()
    if order.gateway == "mock":
        return s.payments_mock and signature == "mock"
    expected = hmac.new(s.razorpay_key_secret.encode(), f"{order.gateway_order_id}|{payment_id}".encode(),
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def webhook_signature_ok(body: bytes, signature: str) -> bool:
    secret = get_settings().razorpay_webhook_secret
    if not secret:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def fulfil_order(db: Session, order: Order, payment_id: str) -> Order:
    """Mark paid and deliver (wallet credit or plan). Safe to call twice."""
    order = db.get(Order, order.id, with_for_update=True)
    if order.status == "paid":
        db.rollback()
        return order
    user = db.get(User, order.user_id)
    order.status, order.gateway_payment_id, order.paid_at = "paid", payment_id, utcnow()
    if order.purpose == "wallet_topup":
        wallet_post(db, user, order.amount_paise, "topup", order.gateway_order_id, "Wallet top-up")
    elif order.purpose == "plan":
        grant_plan(db, user, db.get(Plan, order.plan_id), "payment")
    db.commit()
    return order
