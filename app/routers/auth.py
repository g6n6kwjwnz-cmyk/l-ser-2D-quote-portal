"""Pantalla de acceso y cierre de sesión."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import COOKIE_NAME, authenticate, create_session, destroy_session
from ..config import settings
from ..db import get_db
from ..models import User
from ..web import templates

router = APIRouter(tags=["acceso"])


def _safe_next(target: str | None) -> str:
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


@router.get("/login")
def login_form(request: Request, next: str = "/", db: Session = Depends(get_db)):
    no_users = db.scalar(select(User).limit(1)) is None
    return templates.TemplateResponse(request, "login.html", {"next": _safe_next(next), "no_users": no_users})


@router.post("/login")
async def login(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    email, password = str(form.get("email", "")), str(form.get("password", ""))
    nxt = _safe_next(str(form.get("next", "/")))
    user, error = authenticate(db, email, password)
    if not user:
        return templates.TemplateResponse(request, "login.html", {
            "next": nxt, "error": error, "email": email.strip()[:254], "no_users": False}, status_code=401)
    token = create_session(db, user, request.headers.get("user-agent", ""))
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie(COOKIE_NAME, token, max_age=settings.session_hours * 3600, httponly=True,
                    secure=settings.cookie_secure, samesite="lax", path="/")
    return resp


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    destroy_session(db, request.cookies.get(COOKIE_NAME))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp
