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
