"""Autenticación: usuarios con contraseña en hash scrypt, sesiones en servidor y bloqueo por intentos fallidos.

- Las contraseñas nunca se guardan en claro: scrypt (stdlib) con sal aleatoria por usuario.
- La cookie solo lleva un token aleatorio; en BD se guarda su SHA-256 (si se filtra la BD, no sirve para entrar).
- Tras `max_failed_logins` fallos seguidos la cuenta se bloquea `lockout_minutes` minutos.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import User, UserSession

COOKIE_NAME = "portal_session"
_SCRYPT = dict(n=2 ** 15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)
MIN_PASSWORD_LEN = 10


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:  # SQLite devuelve fechas sin zona
        return dt.replace(tzinfo=timezone.utc)
    return dt


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(digest)
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=64 * 1024 * 1024, dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


_DUMMY_HASH = None


def _dummy_verify(password: str) -> None:
    """Igualar el tiempo de respuesta cuando el usuario no existe (evita enumerar usuarios)."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_hex(8))
    verify_password(password, _DUMMY_HASH)


def validate_new_password(password: str) -> str | None:
    if len(password) < MIN_PASSWORD_LEN:
        return f"La contraseña debe tener al menos {MIN_PASSWORD_LEN} caracteres."
    if password.lower() == password or password.upper() == password or not any(c.isdigit() for c in password):
        return "La contraseña debe combinar mayúsculas, minúsculas y números."
    return None


def authenticate(db: Session, email: str, password: str) -> tuple[User | None, str | None]:
    email = (email or "").strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not user.active:
        _dummy_verify(password)
        return None, "Usuario o contraseña incorrectos."
    locked = _aware(user.locked_until)
    if locked and locked > _now():
        minutes = max(1, round((locked - _now()).total_seconds() / 60))
        return None, f"Cuenta bloqueada temporalmente por intentos fallidos. Prueba en {minutes} min."
    if not verify_password(password, user.password_hash):
        user.failed_logins += 1
        if user.failed_logins >= settings.max_failed_logins:
            user.locked_until = _now() + timedelta(minutes=settings.lockout_minutes)
            user.failed_logins = 0
        db.commit()
        return None, "Usuario o contraseña incorrectos."
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = _now()
    db.commit()
    return user, None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: Session, user: User, user_agent: str = "") -> str:
    token = secrets.token_urlsafe(32)
    db.add(UserSession(token_hash=_token_hash(token), user_id=user.id, user_agent=user_agent[:200],
                       expires_at=_now() + timedelta(hours=settings.session_hours)))
    # limpieza de sesiones caducadas
    for s in db.scalars(select(UserSession).where(UserSession.expires_at < _now())):
        db.delete(s)
    db.commit()
    return token


def user_from_token(db: Session, token: str | None) -> User | None:
    if not token:
        return None
    s = db.scalar(select(UserSession).where(UserSession.token_hash == _token_hash(token)))
    if s is None or _aware(s.expires_at) < _now() or not s.user.active:
        return None
    return s.user


def destroy_session(db: Session, token: str | None) -> None:
    if not token:
        return
    s = db.scalar(select(UserSession).where(UserSession.token_hash == _token_hash(token)))
    if s:
        db.delete(s)
        db.commit()


def change_password(db: Session, user: User, current: str, new: str, repeat: str,
                    keep_token: str | None) -> str | None:
    """Cambio de contraseña por el propio usuario. Devuelve un mensaje de error o None si se cambió."""
    user = db.get(User, user.id)
    if not verify_password(current, user.password_hash):
        return "La contraseña actual no es correcta."
    if new != repeat:
        return "Las contraseñas nuevas no coinciden."
    if new == current:
        return "La nueva contraseña debe ser distinta de la actual."
    if err := validate_new_password(new):
        return err
    if user.email.split("@")[0].lower() in new.lower():
        return "La contraseña no debe contener tu nombre de usuario."
    user.password_hash = hash_password(new)
    user.must_change_password = False
    keep = _token_hash(keep_token) if keep_token else None
    for s in db.scalars(select(UserSession).where(UserSession.user_id == user.id)):
        if s.token_hash != keep:  # cierra las demás sesiones abiertas
            db.delete(s)
    db.commit()
    return None


def set_user(db: Session, email: str, password: str, name: str = "", must_change: bool = True) -> User:
    """Crea o actualiza un usuario (usado por el script de administración).

    Por defecto la contraseña es provisional: el usuario deberá cambiarla en su primer acceso."""
    email = email.strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email, name=name or email.split("@")[0])
        db.add(user)
    elif name:
        user.name = name
    user.password_hash = hash_password(password)
    user.must_change_password = must_change
    user.failed_logins = 0
    user.locked_until = None
    user.active = True
    db.flush()
    # al cambiar la contraseña se cierran sus sesiones abiertas
    for s in db.scalars(select(UserSession).where(UserSession.user_id == user.id)):
        db.delete(s)
    db.commit()
    return user
