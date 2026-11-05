"""PDOA (aggregator / admin) endpoints."""
import csv
import io
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AccessPoint, Order, Plan, User, utcnow
from ..radius import revoke_user
from ..schemas import AccessPointOut, DecisionIn, PlanIn, PlanOut, StatusIn, UserOut
from ..security import require_role

router = APIRouter(prefix="/pdoa", tags=["pdoa (admin)"])
admin = require_role("pdoa")
IST = timezone(timedelta(hours=5, minutes=30))


@router.get("/dashboard")
def dashboard(_: User = Depends(admin), db: Session = Depends(get_db)):
    today = datetime.combine(datetime.now(IST).date(), time.min, IST)
    acct = db.execute(text(
        "SELECT COUNT(*) FILTER (WHERE acctstoptime IS NULL "
        "  AND COALESCE(acctupdatetime, acctstarttime) > NOW() - INTERVAL '15 minutes') AS online_now, "
        "COUNT(*) FILTER (WHERE acctstarttime >= :t) AS sessions_today, "
        "COUNT(DISTINCT username) FILTER (WHERE acctstarttime >= :t) AS users_today, "
        "COALESCE(SUM(COALESCE(acctinputoctets,0)+COALESCE(acctoutputoctets,0)) "
        "  FILTER (WHERE acctstarttime >= :t),0) AS bytes_today FROM radacct"), {"t": today}).mappings().one()
    return {
        "users_total": db.scalar(select(func.count()).select_from(User)),
        "pdos_approved": db.scalar(select(func.count()).select_from(User).where(User.role == "pdo")),
        "pdo_applications_pending": db.scalar(select(func.count()).select_from(User)
                                              .where(User.pdo_status == "pending")),
        "access_points": {s: db.scalar(select(func.count()).select_from(AccessPoint)
                                       .where(AccessPoint.status == s)) for s in ("active", "pending", "suspended")},
        "revenue_today_paise": int(db.scalar(select(func.coalesce(func.sum(Order.amount_paise), 0))
                                             .where(Order.status == "paid", Order.paid_at >= today))),
        **{k: int(v) for k, v in acct.items()},
    }


# ------------------------------------------------------------------ users / PDOs
@router.get("/users", response_model=list[UserOut])
def users(role: str | None = None, pdo_status: str | None = None, phone: str | None = None,
          limit: int = 100, _: User = Depends(admin), db: Session = Depends(get_db)):
    q = select(User).order_by(User.id.desc()).limit(max(1, min(limit, 500)))
    if role:
        q = q.where(User.role == role)
    if pdo_status:
        q = q.where(User.pdo_status == pdo_status)
    if phone:
        q = q.where(User.phone.contains(phone))
    return db.scalars(q).all()


@router.post("/pdo-applications/{user_id}/decision", response_model=UserOut)
def decide_pdo(user_id: int, body: DecisionIn, _: User = Depends(admin), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user or user.pdo_status != "pending":
        raise HTTPException(404, "no_pending_application")
    user.pdo_status = "approved" if body.approve else "rejected"
    if body.approve and user.role == "consumer":
        user.role = "pdo"
    db.commit()
    return user


@router.post("/users/{user_id}/kyc", response_model=UserOut)
def decide_kyc(user_id: int, body: DecisionIn, _: User = Depends(admin), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user or user.kyc_status != "submitted":
        raise HTTPException(404, "no_pending_kyc")
    user.kyc_status = "verified" if body.approve else "rejected"
    db.commit()
    return user


@router.post("/users/{user_id}/active", response_model=UserOut)
def set_user_active(user_id: int, body: DecisionIn, _: User = Depends(admin), db: Session = Depends(get_db)):
    """Block/unblock a user. Blocking also removes their RADIUS credentials."""
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "user_not_found")
    user.is_active = body.approve
    if not body.approve:
        revoke_user(db, user)
    db.commit()
    return user


# --------------------------------------------------------------- access points
@router.get("/access-points", response_model=list[AccessPointOut])
def access_points(status: str | None = None, _: User = Depends(admin), db: Session = Depends(get_db)):
    q = select(AccessPoint).order_by(AccessPoint.id)
    if status:
        q = q.where(AccessPoint.status == status)
    return db.scalars(q).all()


@router.post("/access-points/{ap_id}/status", response_model=AccessPointOut)
def set_ap_status(ap_id: int, body: StatusIn, _: User = Depends(admin), db: Session = Depends(get_db)):
    if body.status not in ("active", "pending", "suspended"):
        raise HTTPException(400, "invalid_status")
    ap = db.get(AccessPoint, ap_id)
    if not ap:
        raise HTTPException(404, "access_point_not_found")
    ap.status = body.status
    db.commit()
    return ap


# ----------------------------------------------------------------------- plans
@router.get("/plans", response_model=list[PlanOut])
def all_plans(_: User = Depends(admin), db: Session = Depends(get_db)):
    return db.scalars(select(Plan).order_by(Plan.sort_order, Plan.id)).all()


@router.post("/plans", response_model=PlanOut)
def create_plan(body: PlanIn, _: User = Depends(admin), db: Session = Depends(get_db)):
    plan = Plan(**body.model_dump())
    db.add(plan)
    db.commit()
    return plan


@router.put("/plans/{plan_id}", response_model=PlanOut)
def update_plan(plan_id: int, body: PlanIn, _: User = Depends(admin), db: Session = Depends(get_db)):
    plan = db.get(Plan, plan_id)
    if not plan:
        raise HTTPException(404, "plan_not_found")
    for k, v in body.model_dump().items():
        setattr(plan, k, v)
    db.commit()
    return plan


# ------------------------------------------------------------------ compliance
@router.get("/sessions/export.csv")
def export_sessions(date_from: date, date_to: date, _: User = Depends(admin), db: Session = Depends(get_db)):
    """Session log for a date range (IST, inclusive): who, which device, which AP, when, how much."""
    start = datetime.combine(date_from, time.min, IST)
    end = datetime.combine(date_to + timedelta(days=1), time.min, IST)
    rows = db.execute(text(
        "SELECT r.username AS mobile, u.name, r.callingstationid AS device_mac, host(r.framedipaddress) AS ip, "
        "r.calledstationid AS nas_id, a.name AS hotspot, host(r.nasipaddress) AS nas_ip, r.acctsessionid AS session_id, "
        "r.acctstarttime AS started_at, r.acctstoptime AS stopped_at, r.acctsessiontime AS seconds, "
        "r.acctinputoctets AS upload_bytes, r.acctoutputoctets AS download_bytes, r.acctterminatecause AS stop_reason "
        "FROM radacct r LEFT JOIN app_users u ON u.phone = r.username "
        "LEFT JOIN app_access_points a ON a.nas_id = r.calledstationid "
        "WHERE r.acctstarttime >= :s AND r.acctstarttime < :e ORDER BY r.acctstarttime"),
        {"s": start, "e": end}).mappings().all()
    buf = io.StringIO()
    cols = ["mobile", "name", "device_mac", "ip", "nas_id", "hotspot", "nas_ip", "session_id", "started_at",
            "stopped_at", "seconds", "upload_bytes", "download_bytes", "stop_reason"]
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow({c: (r[c].astimezone(IST).isoformat() if isinstance(r[c], datetime) else r[c]) for c in cols})
    buf.seek(0)
    name = f"sessions_{date_from}_{date_to}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/registry/export")
def registry_export(_: User = Depends(admin), db: Session = Depends(get_db)):
    """PDOA + AP inventory for the C-DOT Central Registry.
    NOTE: the field names/format must be aligned with the current WANI technical specification
    before submitting; this endpoint gathers the data the registry needs."""
    aps = db.scalars(select(AccessPoint).where(AccessPoint.status == "active")).all()
    return {
        "generated_at": utcnow().isoformat(),
        "spec_alignment": "pending - map fields to the C-DOT WANI registry format",
        "access_points": [{"nas_id": a.nas_id, "name": a.name, "ssid": a.ssid, "mac": a.mac,
                           "lat": a.lat, "lng": a.lng, "address": a.address,
                           "pdo": {"id": a.pdo.id, "business_name": a.pdo.pdo_business_name,
                                   "phone": a.pdo.phone} if a.pdo else None} for a in aps],
    }
