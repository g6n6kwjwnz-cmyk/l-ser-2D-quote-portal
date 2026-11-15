from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text

from .auth import COOKIE_NAME, user_from_token
from .db import Base, SessionLocal, engine
from .routers import auth, catalog, machines, parts, profile, public, requests
from .seed import seed


def _add_missing_columns() -> None:
    """Migración mínima para bases de datos creadas con versiones anteriores."""
    cols = {c["name"] for c in inspect(engine).get_columns("users")}
    if "must_change_password" not in cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN must_change_password BOOLEAN NOT NULL DEFAULT 1"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    _add_missing_columns()
    with SessionLocal() as db:
        seed(db)
    yield


app = FastAPI(title="Portal de ofertas · Corte láser 2D", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(auth.router)
app.include_router(public.router)
app.include_router(requests.router)
app.include_router(profile.router)
app.include_router(parts.router)
app.include_router(machines.router)
app.include_router(catalog.router)


PUBLIC_PATHS = ("/login", "/static/", "/salud", "/solicitar", "/solicitud/")


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if not path.startswith(PUBLIC_PATHS):
        with SessionLocal() as db:
            user = user_from_token(db, request.cookies.get(COOKIE_NAME))
        if user is None:
            if request.method == "GET":
                return RedirectResponse(f"/login?next={quote(path)}", status_code=303)
            return PlainTextResponse("Sesión caducada. Vuelve a iniciar sesión.", status_code=401)
        request.state.user = user
        # contraseña provisional: solo se permite cambiarla o salir
        if user.must_change_password and path not in ("/cuenta/contrasena", "/logout"):
            if request.method == "GET":
                return RedirectResponse("/cuenta/contrasena", status_code=303)
            return PlainTextResponse("Debes cambiar tu contraseña antes de continuar.", status_code=403)
    return await call_next(request)


@app.middleware("http")
async def security(request: Request, call_next):
    # Protección CSRF básica: los envíos deben venir del propio origen
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin and urlparse(origin).netloc != request.url.netloc:
            return PlainTextResponse("Origen no permitido", status_code=403)
    response = await call_next(request)
    response.headers.update({
        "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                                   "script-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "same-origin",
        "X-Frame-Options": "DENY",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    })
    return response


@app.get("/")
def home():
    return RedirectResponse("/peticiones")


@app.get("/salud")
def health():
    return {"ok": True}
