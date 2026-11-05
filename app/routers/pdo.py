"""PDO (hotspot owner) endpoints."""
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import AccessPoint, User
from ..schemas import AccessPointIn, AccessPointOut
from ..security import require_role

router = APIRouter(prefix="/pdo", tags=["pdo"])
pdo_only = require_role("pdo", "pdoa")


def chilli_config(ap: AccessPoint) -> dict:
    """Values to put in /etc/config/chilli on this AP."""
    s = get_settings()
    host = re.sub(r"^https?://", "", s.public_base_url).split("/")[0]
    return {"radiusnasid": ap.nas_id, "radiuscalled": ap.nas_id, "radiusserver1": host,
            "uamserver": f"{s.public_base_url}/chilli", "uamallowed": host, "ssid": ap.ssid}


@router.get("/access-points", response_model=list[AccessPointOut])
def my_access_points(user: User = Depends(pdo_only), db: Session = Depends(get_db)):
    return db.scalars(select(AccessPoint).where(AccessPoint.pdo_id == user.id).order_by(AccessPoint.id)).all()


@router.post("/access-points")
def add_access_point(body: AccessPointIn, user: User = Depends(pdo_only), db: Session = Depends(get_db)):
    """Registers a new AP (status pending until the PDOA activates it). Returns the CoovaChilli
    settings the AP must use."""
    ap = AccessPoint(pdo_id=user.id, name=body.name.strip(), ssid=body.ssid.strip() or "PMWANI-Free",
                     mac=body.mac, lat=body.lat, lng=body.lng, address=body.address,
                     nas_id=f"pdo{user.id}-{secrets.token_hex(3)}", status="pending")
    db.add(ap)
    db.commit()
    return {"access_point": AccessPointOut.model_validate(ap), "chilli_config": chilli_config(ap)}


@router.get("/access-points/{ap_id}/config")
def access_point_config(ap_id: int, user: User = Depends(pdo_only), db: Session = Depends(get_db)):
    ap = db.get(AccessPoint, ap_id)
    if not ap or (ap.pdo_id != user.id and user.role != "pdoa"):
        raise HTTPException(404, "access_point_not_found")
    return chilli_config(ap)


@router.get("/stats")
def pdo_stats(days: int = 30, user: User = Depends(pdo_only), db: Session = Depends(get_db)):
    """Usage per AP over the last N days: sessions, unique users, bytes, minutes."""
    aps = db.scalars(select(AccessPoint).where(AccessPoint.pdo_id == user.id)).all()
    if not aps:
        return {"days": days, "access_points": [], "totals": {"sessions": 0, "users": 0, "bytes": 0}}
    rows = {r["nas_id"]: r for r in db.execute(text(
        "SELECT calledstationid AS nas_id, COUNT(*) AS sessions, COUNT(DISTINCT username) AS users, "
        "COALESCE(SUM(COALESCE(acctinputoctets,0)+COALESCE(acctoutputoctets,0)),0) AS bytes, "
        "COALESCE(SUM(acctsessiontime),0)/60 AS minutes FROM radacct "
        "WHERE calledstationid = ANY(:ids) AND acctstarttime > NOW() - make_interval(days => :d) "
        "GROUP BY calledstationid"), {"ids": [a.nas_id for a in aps], "d": max(1, min(days, 365))}).mappings()}
    per_ap = []
    for ap in aps:
        r = rows.get(ap.nas_id, {})
        per_ap.append({"id": ap.id, "name": ap.name, "nas_id": ap.nas_id, "status": ap.status,
                       "sessions": int(r.get("sessions", 0)), "users": int(r.get("users", 0)),
                       "bytes": int(r.get("bytes", 0)), "minutes": int(r.get("minutes", 0))})
    totals = {k: sum(a[k] for a in per_ap) for k in ("sessions", "bytes", "minutes")}
    return {"days": days, "access_points": per_ap, "totals": totals}
