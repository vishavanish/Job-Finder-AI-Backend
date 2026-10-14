"""
Bootstrap / break-glass role change from the server shell — the only way
to create the FIRST admin, since every API role change needs an admin.

    python -m app.scripts.set_role you@example.com admin

Creates the account if the email is unknown (the person then signs in
with an emailed code or Google). Audited with actor_id = null.
"""
from __future__ import annotations

import argparse
import sys

from app import models  # noqa: F401 — register tables
from app.core.db import SessionLocal
from app.models.user import ROLES, User
from app.services import audit
from app.services.otp import normalize_email


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("email")
    parser.add_argument("role", choices=ROLES)
    args = parser.parse_args(argv)

    email = normalize_email(args.email)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        before = {"role": user.role} if user else None
        if user is None:
            user = User(email=email, role=args.role, auth_provider="invited")
            db.add(user)
            db.flush()
        user.role = args.role
        audit.record(db, actor_id=None, action="user.role_set_cli", target_type="user", target_id=user.id,
                     before=before, after={"role": args.role, "email": email})
        db.commit()
        print(f"{email} is now {args.role} (id {user.id})")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
