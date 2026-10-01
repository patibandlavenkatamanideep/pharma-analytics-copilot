"""HTTP surface.

The API exposes questions, not queries. There is deliberately no endpoint that
accepts SQL, a role, a scope, a user id or a metric formula: every one of those
is derived server-side from a verified session.

SQL and technical detail are hidden by default. A user may ask to see the SQL,
and then they see only the statement their own principal was authorized to run.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.api.guard import RequestGuard
from app.auth import identity
from app.auth.policy import Principal
from app.config import get_settings
from app.conversation.state import (
    ConversationAccessError,
    list_conversations,
    load_history,
)
from app.db import close_pools, verify_runtime_role_safety
from app.llm.planner import build_planner
from app.conversation import runs
from app.pipeline import Pipeline, to_payload

log = logging.getLogger(__name__)

_pipeline: Pipeline | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    # Refuse to serve if the database boundary is not what we think it is. A
    # mis-provisioned deployment fails loudly instead of quietly serving
    # unrestricted data.
    problems = verify_runtime_role_safety()
    if problems:
        raise RuntimeError("database security boundary is not intact: " + "; ".join(problems))

    # The serving process does not need the owner credential: it never
    # creates, alters or loads anything. Holding it anyway turns a compromise
    # of the web process into a compromise of the schema and every table.
    # In the cloud environment that is refused; locally (tests and scripts
    # share one .env) it is a warning.
    for problem in serving_credential_problems(settings):
        if settings.environment == "cloud":
            raise RuntimeError(problem)
        log.warning("%s (allowed only because PAC_ENVIRONMENT=local)", problem)

    global _pipeline
    _pipeline = Pipeline(build_planner(settings))

    # A missing dataset is NOT fatal. The container must come up and report
    # itself unready so an orchestrator can see the state; exiting here would
    # crash-loop a fresh deployment during the window between the first boot
    # and the first data load. /ready returns 503 until a snapshot is
    # published, which is what should gate traffic.
    #
    # A broken security boundary IS fatal (checked above), because that is a
    # misconfiguration no amount of waiting fixes.
    try:
        dataset = _pipeline.current_dataset()
        log.info(
            "serving dataset %s (%s), planner=%s",
            dataset["dataset_id"], dataset["load_mode"], settings.llm_provider,
        )
    except Exception as exc:
        log.warning(
            "starting with no published dataset (%s); /ready will report 503 "
            "until scripts/load_data.py has run", exc,
        )

    yield
    close_pools()


def serving_credential_problems(settings) -> list[str]:
    """Credentials the serving process holds and should not."""
    problems = []
    if settings.db_owner_password:
        problems.append(
            "the serving process holds the database OWNER credential "
            "(PAC_DB_OWNER_PASSWORD); migrations and ingestion run in the jobs "
            "container, which is the only place it belongs")
    return problems


app = FastAPI(title="Pharma Analytics Copilot", lifespan=lifespan, docs_url=None, redoc_url=None)


def _allowed_origins() -> set[str]:
    settings = get_settings()
    origins = {o.strip() for o in settings.allowed_origins.split(",") if o.strip()}
    if settings.environment == "local":
        # The Vite dev server proxies /api with changeOrigin, so the backend
        # sees its own host while the browser sends the dev server's origin.
        origins |= {"http://localhost:5173", "http://127.0.0.1:5173"}
    return origins


app.add_middleware(RequestGuard, max_body_bytes=get_settings().max_request_bytes,
                   allowed_origins=_allowed_origins())


def pipeline() -> Pipeline:
    if _pipeline is None:                       # pragma: no cover
        raise HTTPException(status_code=503, detail="starting up")
    return _pipeline


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def session_token(request: Request) -> str | None:
    """Read the session cookie under its CONFIGURED name.

    Previously this was a `pac_session: Cookie()` parameter, which makes
    FastAPI read whichever cookie is named after the parameter. Login and
    logout used settings.cookie_name, so configuring any other name silently
    broke authentication: the cookie was set and cleared under one name and
    looked for under another. One accessor now, used by every path.
    """
    return request.cookies.get(get_settings().cookie_name)


def current_principal(request: Request, response: Response) -> Principal:
    """Resolve the caller. The browser supplies only an opaque token.

    A token past the rotation window is replaced here, on the response of the
    request that used it, with the same absolute expiry.
    """
    token = session_token(request)
    principal, due = identity.resolve_session(token)
    if principal is None:
        raise HTTPException(status_code=401, detail="Not signed in.")
    if due and token:
        rotated = identity.rotate(token)
        if rotated is not None:
            _set_session_cookie(response, *rotated)
    return principal


def _set_session_cookie(response: Response, token: str, expires_at) -> None:
    from datetime import datetime, timezone

    settings = get_settings()
    remaining = int((expires_at - datetime.now(timezone.utc)).total_seconds())
    response.set_cookie(
        key=settings.cookie_name,
        value=token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        # What is left of the ABSOLUTE lifetime, so a rotated cookie cannot
        # outlive the session it belongs to.
        max_age=max(remaining, 0),
        path="/",
    )


CurrentUser = Annotated[Principal, Depends(current_principal)]


class LoginRequest(BaseModel):
    email: str = Field(max_length=200)
    password: str = Field(max_length=400)


@app.post("/api/login")
def login(body: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    try:
        session, principal = identity.authenticate(
            body.email, body.password,
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None,
        )
    except identity.RateLimited as exc:
        # 429, not 401: the credentials were never checked, and a client that
        # cannot tell the difference will keep hammering.
        raise HTTPException(status_code=429, detail=str(exc)) from None
    except identity.AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from None

    _set_session_cookie(response, session.token, session.expires_at)
    return {"user": _describe(principal)}


@app.post("/api/logout")
def logout(request: Request, response: Response) -> dict[str, str]:
    identity.revoke(session_token(request))
    response.delete_cookie(get_settings().cookie_name, path="/")
    return {"status": "signed out"}


@app.get("/api/me")
def me(user: CurrentUser) -> dict[str, Any]:
    dataset = pipeline().current_dataset()
    return {
        "user": _describe(user),
        "dataset": {
            "id": dataset["dataset_id"],
            "mode": dataset["load_mode"],
            "latest_month": dataset["reporting_anchor"].get("max_period_mo"),
            "latest_quarter": dataset["reporting_anchor"].get("max_period_qtr"),
            "rows": dataset["row_counts"],
        },
    }


def _describe(principal: Principal) -> dict[str, Any]:
    return {
        "name": principal.full_name,
        "email": principal.email,
        "role": principal.role,
        "scope": principal.scope_description,
        "can_view_pricing": principal.wac_authorized,
    }


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------

class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    conversation_id: str | None = Field(default=None, max_length=64)
    # The user may ask to see the SQL. They still only ever see the statement
    # their own principal was authorized to run.
    include_sql: bool = False


@app.post("/api/ask")
def ask(
    body: AskRequest,
    user: CurrentUser,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", max_length=128, pattern=r"^[A-Za-z0-9_\-:.]{8,128}$"),
    ] = None,
) -> dict[str, Any]:
    try:
        result = pipeline().ask(
            user, body.question,
            conversation_id=body.conversation_id,
            include_sql=body.include_sql,
            idempotency_key=idempotency_key,
        )
    except ConversationAccessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except runs.RunBusy as exc:
        # Defined overlap behaviour: one live request per conversation. The
        # client retries; with the same key it gets this request's outcome.
        raise HTTPException(status_code=409, detail={
            "code": "same_request_running" if exc.same_request else "conversation_busy",
            "message": str(exc)}) from None
    except runs.IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail={
            "code": "idempotency_key_reused", "message": str(exc)}) from None
    except runs.ReplayUnavailable as exc:
        raise HTTPException(status_code=403, detail={
            "code": "access_changed", "message": str(exc)}) from None
    except runs.QuotaExceeded as exc:
        raise HTTPException(status_code=429, detail={
            "code": "rate_limited", "message": str(exc)},
            headers={"Retry-After": str(exc.retry_after)}) from None
    except Exception:
        # An unhandled failure still has to be investigable. Without an id in
        # the response there is nothing to connect the user's report to the
        # log line, so the message was a dead end for both sides. The id is
        # generated here, logged with the traceback, and returned on its own --
        # it identifies the log entry and discloses nothing about the cause.
        failure_id = uuid.uuid4().hex[:16]
        log.exception("unhandled pipeline failure (request_id=%s)", failure_id)
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Something went wrong answering that. Please try again.",
                "request_id": failure_id,
            },
        ) from None

    # Built once, by the pipeline, so the copy stored for idempotent replay
    # is exactly this body.
    return result.payload or to_payload(result, body.include_sql)


class CancelRequest(BaseModel):
    run_id: str | None = Field(default=None, max_length=64)
    idempotency_key: str | None = Field(default=None, max_length=128)


@app.post("/api/runs/cancel")
def cancel_run(body: CancelRequest, user: CurrentUser) -> dict[str, Any]:
    """Ask one of your own running requests to stop at its next step. By run
    id, or by the Idempotency-Key it was sent with -- the run id is not known
    until the response arrives. Someone else's run is indistinguishable from
    one that does not exist."""
    if not (body.run_id or body.idempotency_key):
        raise HTTPException(status_code=422, detail="Name a run_id or an idempotency_key.")
    found = runs.request_cancel(user, run_id=body.run_id,
                                idempotency_key=body.idempotency_key)
    if not found:
        raise HTTPException(status_code=404, detail="No running request by that name.")
    return {"status": "cancel_requested"}


@app.get("/api/runs/{run_id}")
def run_state(run_id: str, user: CurrentUser) -> dict[str, Any]:
    """A request's status, for its owner under their current access. Anyone
    else gets the same 404 as for a run that does not exist."""
    found = runs.run_status(user, run_id)
    if found is None:
        raise HTTPException(status_code=404, detail="That request does not exist.")
    return found


@app.get("/api/conversations")
def conversations(user: CurrentUser) -> dict[str, Any]:
    return {"conversations": list_conversations(user)}


@app.get("/api/conversations/{conversation_id}")
def conversation(conversation_id: str, user: CurrentUser) -> dict[str, Any]:
    history = load_history(user, conversation_id)
    if not history:
        # Same response whether it is empty, missing, or someone else's.
        raise HTTPException(status_code=404, detail="That conversation does not exist.")
    return {"conversation_id": conversation_id, "turns": history}


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> JSONResponse:
    """Ready only when a published dataset exists and the boundary is intact."""
    try:
        dataset = pipeline().current_dataset()
    except Exception as exc:
        return JSONResponse({"status": "not ready", "reason": str(exc)[:200]}, status_code=503)
    return JSONResponse({"status": "ready", "dataset": dataset["dataset_id"]})


@app.exception_handler(ConversationAccessError)
def _conversation_denied(request: Request, exc: ConversationAccessError) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=status.HTTP_404_NOT_FOUND)


def mount_frontend() -> None:
    """Serve the built UI from the same origin, so the cookie needs no CORS."""
    import pathlib

    dist = pathlib.Path(__file__).resolve().parent.parent.parent / "web" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="web")
    else:
        log.warning("web/dist not built; API only")


mount_frontend()
