"""Helpers shared by the security tests. Fixtures live in conftest.py."""

from __future__ import annotations

import json

from app.db import owner_transaction


class Identity:
    """A throwaway user plus its password, mutable for the duration of a test."""

    def __init__(self, user_id: str, email: str, password: str):
        self.user_id = user_id
        self.email = email
        self.password = password

    def change(self, **columns) -> None:
        """Change this user's scope, as an administrator would."""
        assignments = ", ".join(f"{k} = %s" for k in columns)
        with owner_transaction() as cur:
            cur.execute(
                f"UPDATE users SET {assignments} WHERE user_id = %s",
                (*columns.values(), self.user_id),
            )


def sign_in(client, identity: Identity):
    client.cookies.clear()
    r = client.post("/api/login",
                    json={"email": identity.email, "password": identity.password})
    assert r.status_code == 200, r.text
    return r


def ask(client, question: str, conversation_id: str | None = None):
    body = {"question": question}
    if conversation_id:
        body["conversation_id"] = conversation_id
    r = client.post("/api/ask", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def has_money(payload: dict) -> bool:
    """Does this response show a currency amount anywhere a user would read?"""
    return "$" in json.dumps(payload)


