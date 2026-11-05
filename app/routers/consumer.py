"""Consumer endpoints: hotspots, plans, wallet, payments, Wi-Fi login, sessions."""
import json
import math

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload

from .. import billing
from ..config import get_settings
from ..db import get_db
from ..models import AccessPoint, Order, Plan, Subscription, User, WalletTransaction, WifiLogin, utcnow
from ..radius import authorize_user, chilli_logon_url, nas_status, valid_chilli_params
from ..schemas import (BuyPlanIn, HotspotOut, OrderOut, PaymentVerifyIn, PlanOut, SubscriptionOut, TopupIn,
                       WalletOut, WifiLoginIn, WifiLoginOut, WifiStatusOut)
from ..security import current_user

router = APIRouter()


def _km(lat1, lng1, lat2, lng2) -> float:
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lng2 - lng1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(a))


def _order_out(order: Order) -> OrderOut:
    s = get_settings()
    return OrderOut(order_id=order.id, gateway=order.gateway, gateway_order_id=order.gateway_order_id,
                    amount_paise=order.amount_paise, status=order.status,
                    razorpay_key_id=None if order.gateway == "mock" else s.razorpay_key_id)


# ---------------------------------------------------------------- hotspots
@router.get("/hotspots", response_model=list[HotspotOut], tags=["hotspots"])
def hotspots(lat: float | None = None, lng: float | None = None, radius_km: float = 25,
             db: Session = Depends(get_db)):
    """Active hotspots, nearest first when lat/lng are given. `online` = had user activity recently."""
    status = nas_status(db)
    out = []
    for ap in db.scalars(select(AccessPoint).where(AccessPoint.status == "active")):
        dist = None
        if lat is not None and lng is not None and ap.lat is not None and ap.lng is not None:
            dist = round(_km(lat, lng, ap.lat, ap.lng), 2)
            if dist > radius_km:
                continue
        st = status.get(ap.nas_id, {})
        out.append(HotspotOut(id=ap.id, name=ap.name, ssid=ap.ssid, nas_id=ap.nas_id, lat=ap.lat, lng=ap.lng,
                              address=ap.address, distance_km=dist, online=st.get("online", False),
                              active_sessions=st.get("active_sessions", 0)))
    out.sort(key=lambda h: (h.distance_km is None, h.distance_km or 0))
    return out


# ------------------------------------------------------------------- plans
@router.get("/plans", response_model=list[PlanOut], tags=["plans"])
def plans(db: Session = Depends(get_db)):
    return db.scalars(select(Plan).where(Plan.is_active).order_by(Plan.sort_order, Plan.price_paise)).all()


@router.post("/plans/{plan_id}/buy", tags=["plans"])
def buy_plan(plan_id: int, body: BuyPlanIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """method=free (₹0 plans), wallet (pay from balance) or gateway (returns a payment order)."""
    plan = db.get(Plan, plan_id)
    if not plan or not plan.is_active:
        raise HTTPException(404, "plan_not_found")
    if plan.price_paise == 0 or body.method == "free":
        sub = billing.claim_free_plan(db, user, plan)
    elif body.method == "wallet":
        sub = billing.buy_with_wallet(db, user, plan)
    elif body.method == "gateway":
        order = billing.create_order(db, user, "plan", plan.price_paise, plan)
        return {"order": _order_out(order)}
    else:
        raise HTTPException(400, "invalid_method")
    db.refresh(sub)
    return {"subscription": SubscriptionOut.model_validate(sub)}


@router.get("/subscriptions", response_model=list[SubscriptionOut], tags=["plans"])
def subscriptions(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return db.scalars(select(Subscription).options(selectinload(Subscription.plan))
                      .where(Subscription.user_id == user.id).order_by(Subscription.id.desc()).limit(50)).all()


# ------------------------------------------------------------------ wallet
@router.get("/wallet", response_model=WalletOut, tags=["wallet & payments"])
def wallet(user: User = Depends(current_user), db: Session = Depends(get_db)):
    txs = db.scalars(select(WalletTransaction).where(WalletTransaction.user_id == user.id)
                     .order_by(WalletTransaction.id.desc()).limit(50)).all()
    return WalletOut(balance_paise=billing.wallet_balance(db, user.id), transactions=txs)


@router.post("/wallet/topup", response_model=OrderOut, tags=["wallet & payments"])
def topup(body: TopupIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Creates a payment order. Open Razorpay Checkout with it, then call /payments/verify."""
    return _order_out(billing.create_order(db, user, "wallet_topup", body.amount_paise))


@router.post("/payments/verify", response_model=OrderOut, tags=["wallet & payments"])
def verify_payment(body: PaymentVerifyIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Call after Razorpay Checkout succeeds. In mock mode send razorpay_signature='mock'."""
    order = db.get(Order, body.order_id)
    if not order or order.user_id != user.id:
        raise HTTPException(404, "order_not_found")
    if not billing.checkout_signature_ok(order, body.razorpay_payment_id, body.razorpay_signature):
        raise HTTPException(400, "invalid_signature")
    return _order_out(billing.fulfil_order(db, order, body.razorpay_payment_id))


@router.post("/payments/razorpay/webhook", include_in_schema=False)
async def razorpay_webhook(request: Request, x_razorpay_signature: str = Header(""),
                           db: Session = Depends(get_db)):
    """Backup path if the app closes before /payments/verify. Configure in the Razorpay dashboard
    for events payment.captured and order.paid."""
    body = await request.body()
    if not billing.webhook_signature_ok(body, x_razorpay_signature):
        raise HTTPException(400, "invalid_signature")
    event = json.loads(body)
    payment = event.get("payload", {}).get("payment", {}).get("entity", {})
    if event.get("event") in ("payment.captured", "order.paid") and payment.get("order_id"):
        order = db.scalar(select(Order).where(Order.gateway_order_id == payment["order_id"]))
        if order and payment.get("amount") == order.amount_paise:
            billing.fulfil_order(db, order, payment.get("id", ""))
    return {"ok": True}


# -------------------------------------------------------------- Wi-Fi login
@router.post("/wifi/login", response_model=WifiLoginOut, tags=["wifi"])
def wifi_login(body: WifiLoginIn, request: Request, user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    """In-app hotspot login. The app reads uamip/uamport/challenge/mac from CoovaChilli's redirect,
    calls this, then opens `logon_url` over the Wi-Fi network."""
    if not valid_chilli_params(body.uamip, body.uamport, body.challenge, body.mac):
        raise HTTPException(422, "invalid_hotspot_parameters")
    sub, remaining = billing.current_subscription(db, user)
    if not sub:
        raise HTTPException(402, "no_active_plan")
    password, timeout = authorize_user(db, user, sub, remaining)
    db.add(WifiLogin(user_id=user.id, subscription_id=sub.id, nas_id=body.nasid, client_mac=body.mac,
                     channel="app", user_agent=request.headers.get("user-agent", "")[:255]))
    db.commit()
    userurl = body.userurl if (body.userurl or "").startswith(("http://", "https://")) else None
    return WifiLoginOut(logon_url=chilli_logon_url(body.uamip, body.uamport, body.challenge, user.phone,
                                                   password, userurl),
                        session_timeout=timeout, remaining_bytes=remaining,
                        plan=PlanOut.model_validate(sub.plan))


@router.get("/wifi/status", response_model=WifiStatusOut, tags=["wifi"])
def wifi_status(user: User = Depends(current_user), db: Session = Depends(get_db)):
    sub, remaining = billing.current_subscription(db, user)
    row = db.execute(text(
        "SELECT acctsessionid, calledstationid, callingstationid, host(framedipaddress) AS ip, acctstarttime, "
        "COALESCE(acctsessiontime,0) AS seconds, COALESCE(acctinputoctets,0) AS upload, "
        "COALESCE(acctoutputoctets,0) AS download FROM radacct WHERE username = :u AND acctstoptime IS NULL "
        "AND COALESCE(acctupdatetime, acctstarttime) > NOW() - make_interval(mins => :m) "
        "ORDER BY acctstarttime DESC LIMIT 1"),
        {"u": user.phone, "m": get_settings().hotspot_online_minutes}).mappings().first()
    return WifiStatusOut(
        subscription=SubscriptionOut.model_validate(sub) if sub else None,
        remaining_bytes=remaining,
        seconds_left=int((sub.expires_at - utcnow()).total_seconds()) if sub else None,
        online_now=row is not None, current_session=dict(row) if row else None)


@router.get("/sessions", tags=["wifi"])
def sessions(limit: int = 50, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """The user's Wi-Fi sessions (from RADIUS accounting). upload/download in bytes."""
    names = {ap.nas_id: ap.name for ap in db.scalars(select(AccessPoint))}
    rows = db.execute(text(
        "SELECT radacctid AS id, acctsessionid AS session_id, calledstationid AS nas_id, "
        "callingstationid AS device_mac, host(framedipaddress) AS ip, acctstarttime AS started_at, "
        "acctstoptime AS stopped_at, COALESCE(acctsessiontime,0) AS seconds, "
        "COALESCE(acctinputoctets,0) AS upload, COALESCE(acctoutputoctets,0) AS download, "
        "acctterminatecause AS stop_reason FROM radacct WHERE username = :u "
        "ORDER BY acctstarttime DESC NULLS LAST LIMIT :n"),
        {"u": user.phone, "n": max(1, min(limit, 200))}).mappings()
    return [{**r, "hotspot_name": names.get(r["nas_id"])} for r in rows]
