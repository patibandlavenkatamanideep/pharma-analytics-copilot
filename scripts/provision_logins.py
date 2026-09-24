#!/usr/bin/env python3
"""Provision evaluator logins.

Passwords come from the environment or are generated. Generated passwords are
printed ONCE to stdout and written to a gitignored file; nothing is committed.

  python3 scripts/provision_logins.py --demo
  python3 scripts/provision_logins.py --user U001 --password "$SOME_ENV_VAR"

--demo provisions one account per role, chosen from the supplied users table so
that each has a genuinely different scope. Whether an account can actually see
rows depends on the loaded dataset: under the seed fixture most RAM territories
match no ZIP at all (docs/ASSUMPTIONS.md#a5), so --demo prefers users whose
assignment exists in the currently loaded zip_territory.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import secrets
import sys

from app.auth.identity import set_credential
from app.db import auth_transaction, close_pools, owner_transaction

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "evaluator_logins.json"       # gitignored


def pick_demo_users() -> list[dict]:
    """One usable account per role, preferring assignments present in the data.

    Uses the owner connection: pac_auth_login deliberately has no privilege on
    zip_territory, because the identity role has no business reading business
    data. Provisioning is an administrative task, not a request path.
    """
    with owner_transaction() as cur:
        cur.execute(
            """
            SELECT u.user_id, u.email, u.full_name, u.role,
                   u.territory_name, u.region_name, u.can_view_wac,
                   CASE
                     WHEN u.role = 'exec' THEN 1
                     WHEN u.role = 'director' AND EXISTS (
                          SELECT 1 FROM zip_territory z
                          WHERE z.region_name = u.region_name) THEN 1
                     WHEN u.role = 'ram' AND EXISTS (
                          SELECT 1 FROM zip_territory z
                          WHERE z.territory_name = u.territory_name) THEN 1
                     ELSE 0
                   END AS resolvable
            FROM users u
            ORDER BY u.role, resolvable DESC, u.user_id
            """
        )
        rows = cur.fetchall()

    chosen: dict[str, dict] = {}
    for row in rows:
        if row["role"] not in chosen:
            chosen[row["role"]] = row
    return list(chosen.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="one account per role")
    ap.add_argument("--user", help="a specific user_id from the supplied users table")
    ap.add_argument("--password", help="password; generated when omitted")
    args = ap.parse_args()

    if not args.demo and not args.user:
        ap.error("pass --demo or --user")

    targets = pick_demo_users() if args.demo else None
    if targets is None:
        with auth_transaction() as cur:
            cur.execute(
                "SELECT user_id, email, full_name, role, territory_name, region_name, "
                "can_view_wac FROM users WHERE user_id = %s",
                (args.user,),
            )
            row = cur.fetchone()
        if row is None:
            print(f"no such user_id: {args.user}", file=sys.stderr)
            return 1
        targets = [row]

    issued = []
    for row in targets:
        password = args.password or secrets.token_urlsafe(12)
        set_credential(row["user_id"], password)
        issued.append(
            {
                "user_id": row["user_id"],
                "email": row["email"],
                "full_name": row["full_name"],
                "role": row["role"],
                "scope": row.get("territory_name") or row.get("region_name") or "all territories",
                "can_view_wac": bool(row["can_view_wac"]),
                "resolvable_in_loaded_data": bool(row.get("resolvable", 1)),
                "password": password,
            }
        )

    OUT.write_text(json.dumps(issued, indent=2))
    OUT.chmod(0o600)

    print(f"provisioned {len(issued)} login(s); written to {OUT} (0600, gitignored)\n")
    for item in issued:
        warn = "" if item["resolvable_in_loaded_data"] else "   [assignment matches no ZIP in the loaded dataset]"
        print(f"  {item['role']:<9} {item['email']:<34} scope={item['scope']:<18} "
              f"wac={'yes' if item['can_view_wac'] else 'no'}{warn}")
        print(f"            password: {item['password']}")
    close_pools()
    return 0


if __name__ == "__main__":
    sys.exit(main())
