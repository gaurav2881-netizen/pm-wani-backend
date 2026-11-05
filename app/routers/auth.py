from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import User
from ..otp import OtpError, issue_otp, normalize_phone, verify_otp
from ..schemas import (KycIn, OtpRequestIn, OtpRequestOut, OtpVerifyIn, PdoApplicationIn, ProfileIn,
                       TokenOut, UserOut)
from ..security import create_token, current_user

router = APIRouter(tags=["auth & profile"])


@router.post("/auth/otp/request", response_model=OtpRequestOut)
def otp_request(body: OtpRequestIn, db: Session = Depends(get_db)):
    phone = normalize_phone(body.phone)
    if not phone:
        raise HTTPException(422, "invalid_phone")
    try:
        issued = issue_otp(db, phone)
    except OtpError as e:
        raise HTTPException(429 if e.code == "too_many_requests" else 502, e.code)
    return OtpRequestOut(request_id=issued.request_id, expires_in=get_settings().otp_ttl_seconds,
                         demo_otp=issued.demo_code)


@router.post("/auth/otp/verify", response_model=TokenOut)
def otp_verify(body: OtpVerifyIn, db: Session = Depends(get_db)):
    try:
        user = verify_otp(db, body.request_id, body.otp)
    except OtpError as e:
        raise HTTPException(400, e.code)
    return TokenOut(access_token=create_token(user), user=UserOut.model_validate(user))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)):
    return user


@router.patch("/me", response_model=UserOut)
def update_me(body: ProfileIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if body.name is not None:
        user.name = body.name.strip() or None
    if body.email is not None:
        user.email = body.email.strip() or None
    db.commit()
    return user


@router.post("/me/kyc", response_model=UserOut)
def submit_kyc(body: KycIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Records a KYC submission. Only the doc type and last 4 digits are stored.
    Real verification (e.g. DigiLocker / an eKYC provider) plugs in here later."""
    user.kyc_doc_type = body.doc_type.lower()
    user.kyc_doc_last4 = body.doc_number.strip()[-4:]
    if not user.name:
        user.name = body.name_on_doc.strip()
    user.kyc_status = "submitted"
    db.commit()
    return user


@router.post("/me/pdo-application", response_model=UserOut)
def apply_pdo(body: PdoApplicationIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role != "consumer":
        raise HTTPException(400, "already_pdo_or_admin")
    if user.pdo_status == "pending":
        raise HTTPException(400, "application_pending")
    user.pdo_business_name = body.business_name.strip()
    user.pdo_address = body.address.strip()
    user.pdo_status = "pending"
    db.commit()
    return user
