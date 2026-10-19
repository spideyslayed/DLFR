from datetime import datetime, timedelta, timezone
from jose import jwt, JWTError
from pwdlib import PasswordHash
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from sqlalchemy import select
from .config import settings
from .database import get_db
from .models import User

ALGORITHM = "HS256"
password_hash = PasswordHash.recommended()
bearer_scheme = HTTPBearer(auto_error=False)

def hash_password(password: str) -> str:
    return password_hash.hash(password)

def verify_password(password: str, hashed: str) -> bool:
    return password_hash.verify(password, hashed)

def create_access_token(user_id: int, role: str) -> str:
    expiry = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    return jwt.encode({"sub": str(user_id), "role": role, "exp": expiry}, settings.jwt_secret, algorithm=ALGORITHM)

def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired login",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorized
    try:
        payload = jwt.decode(
            credentials.credentials, settings.jwt_secret, algorithms=[ALGORITHM]
        )
        subject = payload.get("sub")
        if not subject:
            raise unauthorized
        user = db.get(User, int(subject))
    except (JWTError, ValueError, TypeError):
        raise unauthorized
    if not user or not user.is_active:
        raise unauthorized
    return user

def require_owner(user: User = Depends(get_current_user)) -> User:
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="Owner access required")
    return user

def require_staff(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> User:
    if user.role == "owner":
        return user
    if user.role == "admin":
        # There is one DLFR workspace. Admin staff operate inside the owner's
        # workspace instead of creating a separate register under their own ID.
        workspace_owner = db.scalar(select(User).where(User.role == "owner").limit(1))
        if workspace_owner:
            return workspace_owner
    raise HTTPException(status_code=403, detail="Business staff access required")
