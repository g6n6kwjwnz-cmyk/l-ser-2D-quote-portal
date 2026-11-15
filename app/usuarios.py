"""Gestión de usuarios desde la terminal del servidor (las contraseñas nunca pasan por el repositorio).

    python -m app.usuarios crear correo@empresa.es [--nombre "Nombre"]   # crea o cambia la contraseña
    python -m app.usuarios listar
    python -m app.usuarios desactivar correo@empresa.es

La contraseña se pide por teclado sin mostrarse. Para automatizar se puede pasar en la variable
de entorno PORTAL_NEW_PASSWORD (no se guarda en ningún sitio, solo su hash).
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from sqlalchemy import select

from .auth import set_user, validate_new_password
from .db import Base, SessionLocal, engine
from .models import User


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.usuarios")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("crear", help="Crear usuario o cambiar su contraseña")
    c.add_argument("email")
    c.add_argument("--nombre", default="")
    c.add_argument("--definitiva", action="store_true",
                   help="No obligar a cambiar la contraseña en el primer acceso")
    sub.add_parser("listar")
    d = sub.add_parser("desactivar")
    d.add_argument("email")
    args = ap.parse_args(argv)

    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if args.cmd == "crear":
            pwd = os.environ.get("PORTAL_NEW_PASSWORD")
            if not pwd:
                pwd = getpass.getpass("Contraseña: ")
                if pwd != getpass.getpass("Repite la contraseña: "):
                    print("Las contraseñas no coinciden.", file=sys.stderr)
                    return 1
            if err := validate_new_password(pwd):
                print(err, file=sys.stderr)
                return 1
            u = set_user(db, args.email, pwd, args.nombre, must_change=not args.definitiva)
            extra = "" if args.definitiva else " Deberá cambiar la contraseña en su primer acceso."
            print(f"Usuario {u.email} guardado.{extra}")
        elif args.cmd == "listar":
            for u in db.scalars(select(User).order_by(User.email)):
                last = u.last_login_at.strftime("%d/%m/%Y %H:%M") if u.last_login_at else "nunca"
                print(f"{u.email:40} {'activo' if u.active else 'inactivo':9} último acceso: {last}")
        elif args.cmd == "desactivar":
            u = db.scalar(select(User).where(User.email == args.email.strip().lower()))
            if not u:
                print("No existe.", file=sys.stderr)
                return 1
            u.active = False
            db.commit()
            print(f"Usuario {u.email} desactivado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
