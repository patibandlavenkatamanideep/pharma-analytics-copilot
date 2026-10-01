# API contract

Version **2** (every `/api` response carries `X-API-Version: 2`). Version 1
was the submitted assessment's API; version 2 adds runs, idempotency,
cancellation, clarification choices and the persistence outcome. Nothing in
version 1's request shapes was removed.

`tests/unit/test_api_contract_doc.py` fails if a route exists that this
document does not describe, or the reverse.

## Conventions

- **Identity** is an opaque, `HttpOnly`, `Secure`, `SameSite=Lax` session
  cookie. No endpoint accepts a user id, role, scope, SQL or metric formula;
  all of them are derived on the server from the session.
- **State-changing requests** (`POST`) must be `application/json`, at most
  64 KiB, and same-origin: a request whose `Origin` (or `Sec-Fetch-Site`)
  names another site is refused with `403 cross_origin` before any route
  runs.
- **Errors** carry `detail.code` (stable, machine-readable) and
  `detail.message` (for people). Codes are listed per endpoint.
- **Not yours and not found are the same answer.** Another user's
  conversation or run returns the same `404` as one that does not exist.

### Session lifecycle

| Bound | Value | Notes |
|---|---|---|
| Absolute | 12 h from sign-in | Never extended, by activity or rotation |
| Idle | 30 min | A session unused this long is dead |
| Rotation | 15 min | An older token is replaced on its next use; `Set-Cookie` carries the new one |
| Rotation grace | 30 s | The old token keeps working for requests already in flight |

Disabling an account, changing its password, or `POST /api/logout` ends
sessions immediately.

## Endpoints

### POST /api/login

`{"email": str, "password": str}` → `200 {"user": {...}}` and the session
cookie.

| Status | Code | Meaning |
|---|---|---|
| 401 | — | Wrong email or password, or an account with no usable scope |
| 429 | — | Too many failed attempts for this email or address |

### GET /api/auth/methods

Public. `{"password": true, "oidc": bool}` — which sign-in methods the
sign-in page should offer.

### GET /api/auth/oidc/start

Only when single sign-on is configured (`PAC_OIDC_ENABLED`), otherwise
`404`. Optional `?next=/path` — honoured only if it is a same-site relative
path. Redirects (`302`) to the provider with `state`, `nonce` and a PKCE
`S256` challenge; all three are held on the server, not in a cookie.

### GET /api/auth/oidc/callback

The provider's redirect target. Validates the single-use `state`, exchanges
the code with the PKCE verifier, verifies the ID token (signature against
the issuer's keys, allowed algorithm, issuer, audience/`azp`, expiry,
nonce), maps the verified `(issuer, subject)` to an account, sets the
session cookie and redirects (`303`) to `next`.

| Status | Code | Meaning |
|---|---|---|
| 400 | `invalid_state` | Unknown, reused or expired sign-in attempt |
| 400 | `invalid_token` / `token_rejected` | The token or the code exchange failed verification |
| 400 | `provider_declined` / `provider_unavailable` | The provider refused or could not be reached |
| 403 | `not_linked` | A valid identity with no account link here |
| 403 | `disabled` / `no_scope` | The linked account is disabled or has no usable scope |

### POST /api/logout

No body. Revokes the session and clears the cookie. Always `200`.

### GET /api/me

The signed-in user (`name`, `email`, `role`, `scope`, `can_view_pricing`)
and the published dataset (`id`, `mode`, `latest_month`, `latest_quarter`,
`rows`), with its freshness: `data_through` (the latest transaction date in
the data), `published_at` (when this generation was published),
`incremental` (whether an incremental batch built it) and `last_ingest_at`
(when any feed last delivered a batch, or `null`). See
[INGESTION.md](INGESTION.md#freshness). `401` when not signed in.

### POST /api/ask

```json
{"question": "str, 1-1000 chars",
 "conversation_id": "str | null",
 "include_sql": false}
```

Optional header **`Idempotency-Key`** (8–128 chars of `[A-Za-z0-9_-:.]`).
Send a new key per question and the same key on a retry of that question.

Response `200`:

| Field | Always | Meaning |
|---|---|---|
| `status` | yes | `answered`, `clarify`, `denied`, `error`, `conflict`, `cancelled`, `refresh` |
| `conversation_id` | yes | Continue the conversation by sending it back |
| `message` | yes | Headline, clarification or refusal text |
| `request_id` | yes | Correlates with the audit row and logs |
| `run_id` | yes | This request's run (`GET /api/runs/{run_id}`) |
| `persistence` | yes | `saved`, `not_saved`, `failed`, `conflict` — whether this turn is now part of the conversation |
| `choices` | on some `clarify` | `[{"id", "label", "detail"}]` in display order; reply with "the second one", the number, or a distinguishing detail |
| `alternative` | on some `denied` | What the user can ask instead |
| `answer` | on `answered` | `headline`, `columns`, `rows`, `scope_note`, `period_note`, `warnings`, `notes`, `row_count`, `truncated` |
| `plan`, `sql` | only with `include_sql` | The typed plan, and the SQL this user was authorised to run |
| `replayed` | on a replay | `true` when the response is the stored outcome of an earlier request with the same key |

`refresh` means the data was republished while the question was being
answered: the plan described the old data, so it was not run. Nothing was
recorded; sending the same request again -- with the same key -- answers on
the new data. The interface does this once, automatically.

Interpreting `persistence`: only `saved` means the next turn can build on
this one. `failed` returns the answer but it was not recorded; `conflict`
means the conversation moved on while this was being answered and the
answer is withheld.

| Status | Code | Meaning |
|---|---|---|
| 404 | — | The conversation does not exist or is not yours |
| 409 | `conversation_busy` | Another question in this conversation is being answered |
| 409 | `same_request_running` | This key's request is still running |
| 409 | `idempotency_key_reused` | This key was used for a different request |
| 403 | `access_changed` | The stored outcome was computed under access you no longer have |
| 429 | `rate_limited` | Per-user rate or concurrency limit; honour `Retry-After` |
| 413 / 415 / 403 | `request_too_large` / `unsupported_media_type` / `cross_origin` | Refused by the request guard |
| 500 | — | Unexpected; `detail.request_id` identifies the log entry |

### POST /api/runs/cancel

`{"run_id": str}` or `{"idempotency_key": str}` — the key works before the
run id is known. Cancels one of **your** running requests at its next step;
nothing is recorded for it. `200 {"status": "cancel_requested"}`, or `404`
when there is no running request of yours by that name.

### GET /api/runs/{run_id}

`{"run_id", "conversation_id", "status", "turn_seq", "created_at",
"finished_at"}` for your own run under your current access; `404` otherwise.
Run statuses: `running`, `succeeded`, `failed`, `conflicted`, `abandoned`,
`cancelled`.

### GET /api/conversations

Your conversations under your **current** access, newest first:
`{"conversations": [{"conversation_id", "title", "updated_at"}]}`. A
conversation recorded under access you no longer hold is not listed.

### GET /api/conversations/{conversation_id}

`{"conversation_id", "turns": [{"seq", "question", "answer_text", "status",
"created_at"}]}`, or `404` — the same for missing, someone else's, and
recorded under access you no longer hold.

### DELETE /api/conversations/{conversation_id}

Deletes one of **your** conversations with its turns, cohorts,
clarifications, runs and workflow checkpoints. Works whatever access the
conversation was recorded under: deleting discloses nothing.
`200 {"deleted": true}`.

| Status | Code | Meaning |
|---|---|---|
| 404 | — | No such conversation of yours (the same for someone else's) |
| 409 | `conversation_busy` | A question in it is still being answered |

### GET /api/me/data

Your data as a JSON download (`Content-Disposition: attachment`,
`Cache-Control: no-store`): profile, conversations with turns, cohorts and
their members, clarifications and run records, sessions (times and user
agent only), linked sign-in identities, and a summary of your audit records.
Bound to **current** access. A conversation recorded under access you no
longer hold is withheld whole and counted in `withheld_conversations`. See
[RETENTION.md](RETENTION.md).

### DELETE /api/me/data

Deletes every conversation you own, as above, regardless of the access it
was recorded under. You stay signed in. Security audit records are kept for
the audit retention period; they hold hashes, codes and timings, not
questions or results. `200 {"deleted": {"conversations": n,
"workflow_threads": m}}`, or `409 conversation_busy` while a question is
being answered.

### GET /health

Liveness: `200 {"status": "ok"}` whenever the process serves requests.

### GET /ready

Readiness: `200` only when a published dataset exists; `503` before the
first load, so a container is not sent traffic it cannot serve.
