from app.auth import hash_password, validate_new_password, verify_password
from tests.conftest import TEST_PASSWORD, TEST_USER


def test_password_hashing():
    h = hash_password("Abc-12345-xyz")
    assert h.startswith("scrypt$") and "Abc-12345-xyz" not in h
    assert verify_password("Abc-12345-xyz", h)
    assert not verify_password("abc-12345-xyz", h)
    assert hash_password("Abc-12345-xyz") != h          # sal distinta cada vez


def test_password_policy():
    assert validate_new_password("corta1A")
    assert validate_new_password("todominusculas123")
    assert validate_new_password("Valida-Clave-2027") is None


def test_pages_require_login(client):
    client.post("/logout")
    r = client.get("/maquinas", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=/maquinas")
    assert client.post("/maquinas/1/estado").status_code == 401
    assert client.get("/login").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_login_logout_and_next(client):
    client.post("/logout")
    bad = client.post("/login", data={"email": TEST_USER, "password": "mala"})
    assert bad.status_code == 401 and "incorrectos" in bad.text
    ok = client.post("/login", data={"email": TEST_USER.upper(), "password": TEST_PASSWORD, "next": "/maquinas"},
                     follow_redirects=False)
    assert ok.status_code == 303 and ok.headers["location"] == "/maquinas"
    assert "httponly" in ok.headers["set-cookie"].lower()
    assert TEST_USER in client.get("/maquinas").text
    evil = client.post("/login", data={"email": TEST_USER, "password": TEST_PASSWORD, "next": "//evil.example"},
                       follow_redirects=False)
    assert evil.headers["location"] == "/"
    client.post("/logout")
    assert client.get("/maquinas", follow_redirects=False).status_code == 303


def test_lockout_after_failed_attempts(client):
    client.post("/logout")
    for _ in range(5):
        client.post("/login", data={"email": TEST_USER, "password": "mala"})
    r = client.post("/login", data={"email": TEST_USER, "password": TEST_PASSWORD})
    assert r.status_code == 401 and "bloqueada" in r.text


def test_unknown_user_same_message(client):
    client.post("/logout")
    r = client.post("/login", data={"email": "nadie@example.com", "password": "x"})
    assert r.status_code == 401 and "Usuario o contraseña incorrectos" in r.text


def test_cli_creates_user(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app import usuarios
    engine = create_engine(f"sqlite:///{tmp_path}/cli.db")
    monkeypatch.setattr(usuarios, "engine", engine)
    monkeypatch.setattr(usuarios, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setenv("PORTAL_NEW_PASSWORD", "debil")
    assert usuarios.main(["crear", "a@b.es"]) == 1
    monkeypatch.setenv("PORTAL_NEW_PASSWORD", "Fuerte-Clave-2027")
    assert usuarios.main(["crear", "a@b.es"]) == 0


def test_first_login_forces_password_change(client):
    from app import main
    from app.auth import set_user
    with main.SessionLocal() as db:
        set_user(db, "nuevo@example.com", "Provisional-2027")
    client.post("/logout")
    r = client.post("/login", data={"email": "nuevo@example.com", "password": "Provisional-2027", "next": "/maquinas"},
                    follow_redirects=False)
    assert r.headers["location"] == "/cuenta/contrasena"
    assert client.get("/maquinas", follow_redirects=False).headers["location"] == "/cuenta/contrasena"
    assert client.post("/maquinas/1/estado").status_code == 403
    page = client.get("/cuenta/contrasena")
    assert "contraseña provisional" in page.text
    bad = client.post("/cuenta/contrasena", data={"current": "Provisional-2027", "new": "Provisional-2027",
                                                   "repeat": "Provisional-2027"})
    assert bad.status_code == 422 and "distinta" in bad.text
    bad = client.post("/cuenta/contrasena", data={"current": "mala", "new": "Nueva-Clave-2028", "repeat": "Nueva-Clave-2028"})
    assert bad.status_code == 422
    ok = client.post("/cuenta/contrasena", data={"current": "Provisional-2027", "new": "Nueva-Clave-2028",
                                                  "repeat": "Nueva-Clave-2028"}, follow_redirects=False)
    assert ok.status_code == 303
    assert client.get("/maquinas").status_code == 200          # sigue con sesión y ya puede navegar
    client.post("/logout")
    assert client.post("/login", data={"email": "nuevo@example.com", "password": "Provisional-2027"}).status_code == 401
    assert client.post("/login", data={"email": "nuevo@example.com", "password": "Nueva-Clave-2028"},
                       follow_redirects=False).headers["location"] == "/"


def test_menu_has_change_password(client):
    assert "/cuenta/contrasena" in client.get("/maquinas").text
