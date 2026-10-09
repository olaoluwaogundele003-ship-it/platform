"""Token authentication for the Platform monolith (stdlib only).

Passwords: PBKDF2-HMAC-SHA256 with per-user salt.
Tokens: opaque bearer tokens stored in DB with 30-day expiry.
"""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.database import get_db
from app import models

TOKEN_DAYS = 30
_bearer = HTTPBearer(auto_error=False)


def utcnow():
    return datetime.now(timezone.utc)


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return f"pbkdf2${salt}${dk.hex()}"


def verify_password(password, stored):
    try:
        _, salt, _ = stored.split("$")
        return hmac.compare_digest(hash_password(password, salt), stored)
    except Exception:
        return False


def create_token(db, user_id):
    tok = secrets.token_urlsafe(32)
    db.add(models.AuthToken(
        user_id=user_id, token=tok,
        expires_at=(utcnow() + timedelta(days=TOKEN_DAYS)).replace(tzinfo=None),
    ))
    db.commit()
    return tok


def user_from_token(db, token):
    if not token:
        return None
    row = db.query(models.AuthToken).filter_by(token=token).first()
    if not row:
        return None
    if row.expires_at and row.expires_at < datetime.now(timezone.utc).replace(tzinfo=None):
        db.delete(row)
        db.commit()
        return None
    return db.query(models.User).filter_by(id=row.user_id).first()


def get_current_user(request: Request, db: Session = Depends(get_db)):
    # Bearer header first, then ?token= fallback (for <img>/EventSource simplicity)
    token = None
    auth: HTTPAuthorizationCredentials = None
    try:
        import anyio  # noqa
    except Exception:
        pass
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
    if not token:
        token = request.query_params.get("token")
    if not token:  # legacy localStorage header used by first UI build
        token = request.headers.get("x-session-token")
    user = user_from_token(db, token)
    if not user:
        raise HTTPException(401, "not authenticated — log in first")
    return user


def get_membership(db, group_id, user_id):
    return db.query(models.Membership).filter_by(group_id=group_id, user_id=user_id).first()


def require_member(group_id: int, user, db):
    m = get_membership(db, group_id, user.id)
    if not m:
        raise HTTPException(403, "you are not a member of this group — join with an invite code first")
    return m


def require_admin(group_id: int, user, db):
    m = require_member(group_id, user, db)
    if m.role != "admin":
        raise HTTPException(403, "admins only")
    return m
