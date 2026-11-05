from datetime import timedelta

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import User, utcnow

bearer = HTTPBearer(auto_error=False)


def create_token(user: User) -> str:
    s = get_settings()
    now = utcnow()
    payload = {"sub": str(user.id), "role": user.role, "iat": now, "exp": now + timedelta(days=s.jwt_days)}
    return jwt.encode(payload, s.jwt_secret, algorithm="HS256")


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(bearer),
                 db: Session = Depends(get_db)) -> User:
    if not creds:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not_authenticated")
    try:
        payload = jwt.decode(creds.credentials, get_settings().jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_token")
    user = db.get(User, int(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user_disabled")
    return user


def require_role(*roles: str):
    def checker(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
        return user
    return checker
