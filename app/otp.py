"""OTP issue/verify, shared by the app API and the captive-portal pages."""
import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import OtpRequest, User, utcnow

log = logging.getLogger("otp")
PHONE_RE = re.compile(r"^[6-9]\d{9}$")


def normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw or "")[-10:]
    return digits if PHONE_RE.match(digits) else None


def _hash(phone: str, code: str) -> str:
    return hmac.new(get_settings().jwt_secret.encode(), f"{phone}:{code}".encode(), hashlib.sha256).hexdigest()


class OtpError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class IssuedOtp:
    request_id: str
    demo_code: str | None          # only set in demo mode


def send_sms(phone: str, code: str) -> None:
    s = get_settings()
    if s.sms_provider == "msg91" and s.msg91_authkey and s.msg91_template_id:
        # MSG91 OTP API with a DLT-approved template (the template must contain ##otp##)
        r = httpx.post(
            "https://control.msg91.com/api/v5/otp",
            params={"template_id": s.msg91_template_id, "mobile": f"91{phone}", "otp": code},
            headers={"authkey": s.msg91_authkey},
            timeout=10,
        )
        if r.status_code >= 400:
            log.error("MSG91 error %s: %s", r.status_code, r.text[:200])
            raise OtpError("sms_failed", "Could not send the OTP. Please try again.")
        return
    log.info("DEMO OTP for %s: %s", phone, code)


def issue_otp(db: Session, phone: str) -> IssuedOtp:
    s = get_settings()
    recent = db.scalar(select(func.count()).select_from(OtpRequest).where(
        OtpRequest.phone == phone, OtpRequest.created_at > utcnow() - timedelta(minutes=10)))
    if recent >= s.otp_max_per_10min:
        raise OtpError("too_many_requests", "Too many OTP requests. Please try again in 10 minutes.")
    code = f"{secrets.randbelow(10**6):06d}"
    req = OtpRequest(id=secrets.token_hex(16), phone=phone, code_hash=_hash(phone, code))
    db.add(req)
    db.commit()
    send_sms(phone, code)
    return IssuedOtp(request_id=req.id, demo_code=code if s.otp_demo else None)


def verify_otp(db: Session, request_id: str, code: str) -> User:
    """Returns the (created if new) user. Raises OtpError on failure."""
    s = get_settings()
    req = db.get(OtpRequest, request_id, with_for_update=True)
    if not req:
        raise OtpError("otp_not_found", "This OTP has expired. Please request a new one.")
    age = (utcnow() - req.created_at).total_seconds()
    if req.verified or age > s.otp_ttl_seconds or req.attempts >= s.otp_max_attempts:
        db.rollback()
        raise OtpError("otp_expired", "This OTP has expired. Please request a new one.")
    if not hmac.compare_digest(req.code_hash, _hash(req.phone, (code or "").strip())):
        req.attempts += 1
        db.commit()
        raise OtpError("otp_invalid", "Incorrect OTP. Please try again.")
    req.verified = True
    user = db.scalar(select(User).where(User.phone == req.phone))
    if not user:
        user = User(phone=req.phone)
        db.add(user)
    if req.phone in s.pdoa_phone_set:
        user.role = "pdoa"
    db.commit()
    db.refresh(user)
    return user
