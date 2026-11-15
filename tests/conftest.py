import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session", autouse=True)
def _fixtures():
    from tests import make_fixtures
    make_fixtures.OUT.mkdir(exist_ok=True)
    make_fixtures.plate_with_holes(make_fixtures.OUT / "placa_taladros.dxf")
    make_fixtures.flange_inches(make_fixtures.OUT / "brida_pulgadas.dxf")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from fastapi.testclient import TestClient
    from app import db as dbmod, main
    engine = create_engine(f"sqlite:///{tmp_path}/test.db", connect_args={"check_same_thread": False})
    monkeypatch.setattr(dbmod, "engine", engine)
    monkeypatch.setattr(main, "engine", engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(main, "SessionLocal", Session)

    def _get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()
    main.app.dependency_overrides[dbmod.get_db] = _get_db
    with TestClient(main.app) as c:
        from app.auth import set_user
        with Session() as db:
            set_user(db, TEST_USER, TEST_PASSWORD, must_change=False)
        r = c.post("/login", data={"email": TEST_USER, "password": TEST_PASSWORD}, follow_redirects=False)
        assert r.status_code == 303
        yield c
    main.app.dependency_overrides.clear()


FIXTURES = Path(__file__).parent / "fixtures"
TEST_USER = "test@example.com"
TEST_PASSWORD = "Prueba-Segura-123"
