"""Bridge between app users/plans and FreeRADIUS (radcheck/radreply) + CoovaChilli login URLs."""
import hashlib
import ipaddress
import re
import secrets
from urllib.parse import urlencode

from sqlalchemy import text
from sqlalchemy.orm import Session

from .config import get_settings
from .models import Subscription, User, utcnow

MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}([-:]?[0-9A-Fa-f]{2}){5}$")
HEX32_RE = re.compile(r"^[0-9A-Fa-f]{32}$")
MAX_OCTETS = 2**32 - 1   # ChilliSpot-Max-Total-Octets is a 32-bit attribute


def private_ipv4(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.version == 4 and ip.is_private


def valid_chilli_params(uamip: str, uamport: str, challenge: str, mac: str) -> bool:
    return bool(private_ipv4(uamip or "") and (uamport or "").isdigit() and 0 < int(uamport) < 65536
                and HEX32_RE.match(challenge or "") and MAC_RE.match(mac or ""))


def authorize_user(db: Session, user: User, sub: Subscription, remaining_bytes: int | None) -> tuple[str, int]:
    """Write a one-time RADIUS password + limits for this user. Returns (password, session_timeout).
    Caller commits."""
    s = get_settings()
    plan = sub.plan
    seconds_left = int((sub.expires_at - utcnow()).total_seconds())
    session_timeout = max(60, min(seconds_left, s.max_session_seconds))
    password = secrets.token_urlsafe(18)

    reply = [("Session-Timeout", str(session_timeout)),
             ("Acct-Interim-Interval", str(s.interim_interval))]
    if remaining_bytes is not None:
        reply.append(("ChilliSpot-Max-Total-Octets", str(min(remaining_bytes, MAX_OCTETS))))
    if plan.speed_down_kbps:
        reply.append(("WISPr-Bandwidth-Max-Down", str(plan.speed_down_kbps * 1000)))
    if plan.speed_up_kbps:
        reply.append(("WISPr-Bandwidth-Max-Up", str(plan.speed_up_kbps * 1000)))

    u = user.phone
    db.execute(text("DELETE FROM radcheck WHERE username = :u"), {"u": u})
    db.execute(text("INSERT INTO radcheck (username, attribute, op, value) "
                    "VALUES (:u, 'Cleartext-Password', ':=', :p)"), {"u": u, "p": password})
    db.execute(text("DELETE FROM radreply WHERE username = :u"), {"u": u})
    for attr, value in reply:
        db.execute(text("INSERT INTO radreply (username, attribute, op, value) VALUES (:u, :a, ':=', :v)"),
                   {"u": u, "a": attr, "v": value})
    return password, session_timeout


def revoke_user(db: Session, user: User) -> None:
    db.execute(text("DELETE FROM radcheck WHERE username = :u"), {"u": user.phone})
    db.execute(text("DELETE FROM radreply WHERE username = :u"), {"u": user.phone})


def chilli_logon_url(uamip: str, uamport: str, challenge: str, username: str, password: str,
                     userurl: str | None = None) -> str:
    """CoovaChilli UAM login URL using CHAP (works with any password length)."""
    chal = bytes.fromhex(challenge)
    secret = get_settings().chilli_uamsecret
    if secret:
        chal = hashlib.md5(chal + secret.encode()).digest()
    response = hashlib.md5(b"\x00" + password.encode() + chal).hexdigest()
    query = {"username": username, "response": response}
    if userurl:
        query["userurl"] = userurl
    return f"http://{uamip}:{uamport}/logon?{urlencode(query)}"


# ------------------------------------------------------------ accounting
def nas_status(db: Session) -> dict[str, dict]:
    """Per NAS id (= Called-Station-Id sent by CoovaChilli): last seen time and active sessions."""
    s = get_settings()
    rows = db.execute(text(
        "SELECT calledstationid AS nas, MAX(COALESCE(acctupdatetime, acctstarttime)) AS last_seen, "
        "COUNT(*) FILTER (WHERE acctstoptime IS NULL "
        "  AND COALESCE(acctupdatetime, acctstarttime) > NOW() - make_interval(mins => :m)) AS active "
        "FROM radacct GROUP BY calledstationid"), {"m": s.hotspot_online_minutes}).mappings()
    out = {}
    for r in rows:
        if not r["nas"]:
            continue
        online = r["last_seen"] is not None and (utcnow() - r["last_seen"]).total_seconds() < s.hotspot_online_minutes * 60
        out[r["nas"]] = {"last_seen": r["last_seen"], "active_sessions": int(r["active"]), "online": online}
    return out
