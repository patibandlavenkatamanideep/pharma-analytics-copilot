#!/usr/bin/env python3
"""Create, update or disable the reviewers' accounts the owner approved.

    PAC_REVIEWERS='[{"email": "...", "name": "...", "like": "<user_id>", "password": "..."}]' \\
        python3 scripts/provision_reviewers.py

Anyone may reach the sign-in page; only these accounts may use the
application. Each reviewer gets an account of their own -- no shared
sign-in, no shared conversations -- with the scope (role, territory, region,
pricing) of an existing user the owner names in "like", so what a reviewer
may see is exactly what that user may see, enforced by the same row-level
security. "disabled": true instead of a password disables the account and
ends its sessions at once.

The whole list is applied on every run (adding a reviewer mid-week re-runs
it): a reviewer whose password is unchanged keeps their sessions; a changed
password replaces the credential and ends them.

The list comes from a secret (infra/aws-staging: the reviewers secret, set by
seed-secrets.sh --reviewers); passwords are never printed or logged. Run
after the dataset is loaded, as the owner (the one-shot jobs container).
Output: counts only.

    python3 scripts/provision_reviewers.py --merge-csv reviewers.csv < current.json

is the owner's side (infra/aws-staging/seed-secrets.sh --reviewers): the
list in the secret (stdin) brought in line with a CSV of email,name,like
and an optional disabled column. A reviewer already listed keeps their
password, a new one gets a random one, and one no longer in the CSV is
disabled, never silently left active. The new list goes to stdout (for the
secret), counts to stderr; nothing changes if the CSV is invalid.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import secrets
import sys

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 12


def reviewer_id(email: str) -> str:
    return "rev-" + hashlib.sha256(email.strip().lower().encode()).hexdigest()[:12]


def problems(reviewers: list) -> list[str]:
    found, seen = [], set()
    for i, r in enumerate(reviewers):
        where = f"reviewer {i + 1}"
        if not isinstance(r, dict):
            found.append(f"{where}: not an object")
            continue
        email = str(r.get("email", "")).strip().lower()
        if not EMAIL.match(email):
            found.append(f"{where}: no valid email")
        elif email in seen:
            found.append(f"{where}: email listed twice")
        seen.add(email)
        if not str(r.get("name", "")).strip():
            found.append(f"{where}: no name")
        if not r.get("disabled"):
            if not str(r.get("like", "")).strip():
                found.append(f"{where}: no 'like' (the user whose scope to copy)")
            if len(str(r.get("password", ""))) < MIN_PASSWORD:
                found.append(f"{where}: a password of at least {MIN_PASSWORD} characters is needed")
    return found


def merge(current: list, rows: list[dict]) -> list[dict]:
    """The CSV's reviewers, keeping the passwords already in `current`."""
    known = {str(r.get("email", "")).strip().lower(): r for r in current if isinstance(r, dict)}
    merged, listed = [], set()
    for row in rows:
        email = str(row.get("email") or "").strip().lower()
        name = str(row.get("name") or "").strip()
        listed.add(email)
        if str(row.get("disabled") or "").strip().lower() in ("1", "true", "yes", "y"):
            merged.append({"email": email, "name": name, "disabled": True})
            continue
        password = known.get(email, {}).get("password") or secrets.token_urlsafe(18)
        merged.append({"email": email, "name": name, "like": str(row.get("like") or "").strip(),
                       "password": password})
    for email, r in known.items():
        if email not in listed:
            merged.append({"email": email, "name": r.get("name") or email, "disabled": True})
    return merged


def merge_csv(path: str) -> int:
    try:
        current = json.loads(sys.stdin.read() or "[]")
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            missing = {"email", "name", "like"} - set(reader.fieldnames or [])
            rows = list(reader)
    except (OSError, ValueError, csv.Error) as e:
        print(f"refused, nothing changed: {type(e).__name__}", file=sys.stderr)
        return 2
    if missing or not isinstance(current, list):
        print("refused, nothing changed: the CSV needs the columns email,name,like "
              "(and optionally disabled); the current list must be a JSON list", file=sys.stderr)
        return 2
    merged = merge(current, rows)
    bad = problems(merged)
    if bad:
        print("refused, nothing changed:\n  " + "\n  ".join(bad), file=sys.stderr)
        return 2
    known = {str(r.get("email", "")).strip().lower() for r in current if isinstance(r, dict)}
    active = [r for r in merged if not r.get("disabled")]
    new = sum(r["email"] not in known for r in active)
    json.dump(merged, sys.stdout)
    print(f"reviewers: {len(active)} active ({new} new), {len(merged) - len(active)} disabled",
          file=sys.stderr)
    return 0


def unchanged(stored: str | None, password: str) -> bool:
    """Does the stored credential already hold this password?"""
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerificationError

    if stored is None:
        return False
    try:
        return PasswordHasher().verify(stored, password)
    except (VerificationError, InvalidHashError):
        return False


def main() -> int:
    from app.auth.identity import set_credential, set_disabled
    from app.db import close_pools, owner_transaction

    try:
        reviewers = json.loads(os.environ.get("PAC_REVIEWERS", ""))
    except ValueError:
        print("PAC_REVIEWERS is not JSON", file=sys.stderr)
        return 2
    if not isinstance(reviewers, list):
        print("PAC_REVIEWERS must be a list", file=sys.stderr)
        return 2
    bad = problems(reviewers)
    if bad:
        print("refused, nothing changed:\n  " + "\n  ".join(bad), file=sys.stderr)
        return 2

    active = disabled = 0
    try:
        for r in reviewers:
            email = r["email"].strip().lower()
            uid = reviewer_id(email)
            if r.get("disabled"):
                set_disabled(uid, True)
                disabled += 1
                continue
            with owner_transaction() as cur:
                cur.execute("SELECT role, territory_name, region_name, can_view_wac "
                            "FROM users WHERE user_id = %s", (r["like"],))
                scope = cur.fetchone()
                if scope is None:
                    print(f"refused: no user {r['like']!r} to copy the scope of", file=sys.stderr)
                    return 2
                cur.execute(
                    "INSERT INTO users (user_id, email, full_name, role, territory_name, "
                    "region_name, can_view_wac) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (user_id) DO UPDATE SET email = EXCLUDED.email, "
                    "full_name = EXCLUDED.full_name, role = EXCLUDED.role, "
                    "territory_name = EXCLUDED.territory_name, region_name = EXCLUDED.region_name, "
                    "can_view_wac = EXCLUDED.can_view_wac",
                    (uid, email, r["name"].strip(), scope["role"], scope["territory_name"],
                     scope["region_name"], scope["can_view_wac"]))
                cur.execute("SELECT password_hash FROM app_auth.credentials WHERE user_id = %s",
                            (uid,))
                stored = cur.fetchone()
            if not unchanged(stored and stored["password_hash"], r["password"]):
                set_credential(uid, r["password"])
            set_disabled(uid, False)
            active += 1
    finally:
        close_pools()
    print(f"reviewer accounts: {active} active, {disabled} disabled")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--merge-csv"] and len(sys.argv) == 3:
        sys.exit(merge_csv(sys.argv[2]))
    sys.exit(main())
