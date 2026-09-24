"""HTTP surface.

The API exposes questions, not queries. There is deliberately no endpoint that
accepts SQL, a role, a scope, a user id or a metric formula: every one of those
is derived server-side from a verified session.

SQL and technical detail are hidden by default. A user may ask to see the SQL,
and then they see only the statement their own principal was authorized to run.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

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
from app.pipeline import Pipeline

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


app = FastAPI(title="Pharma Analytics Copilot", lifespan=lifespan, docs_url=None, redoc_url=None)


def pipeline() -> Pipeline:
    if _pipeline is None:                       # pragma: no cover
        raise HTTPException(status_code=503, detail="starting up")
    return _pipeline


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def current_principal(
    pac_session: Annotated[str | None, Cookie()] = None,
) -> Principal:
    """Resolve the caller. The browser supplies only an opaque token."""
    principal = identity.resolve(pac_session)
    if principal is None:
        raise HTTPException(status_code=401, detail="Not signed in.")
    return principal


CurrentUser = Annotated[Principal, Depends(current_principal)]


class LoginRequest(BaseModel):
    email: str = Field(max_length=200)
    password: str = Field(max_length=400)


@app.post("/api/login")
def login(body: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    settings = get_settings()
    try:
        session, principal = identity.authenticate(
            body.email, body.password,
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None,
        )
    except identity.AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from None

    response.set_cookie(
        key=settings.cookie_name,
        value=session.token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.session_ttl_hours * 3600,
        path="/",
    )
    return {"user": _describe(principal)}


@app.post("/api/logout")
def logout(
    response: Response,
    pac_session: Annotated[str | None, Cookie()] = None,
) -> dict[str, str]:
    identity.revoke(pac_session)
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
def ask(body: AskRequest, user: CurrentUser) -> dict[str, Any]:
    try:
        result = pipeline().ask(
            user, body.question,
            conversation_id=body.conversation_id,
            include_sql=body.include_sql,
        )
    except ConversationAccessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except Exception:
        log.exception("unhandled pipeline failure")
        raise HTTPException(
            status_code=500,
            detail="Something went wrong answering that. Please try again.",
        ) from None

    payload: dict[str, Any] = {
        "status": result.status,
        "conversation_id": result.conversation_id,
        "message": result.message,
        "request_id": result.request_id,
    }
    if result.alternative:
        payload["alternative"] = result.alternative
    if body.include_sql:
        # The typed plan is returned alongside the SQL, under the same explicit
        # request. It is strictly less sensitive than the SQL -- it names a
        # metric key, dimensions and filter values, and by construction cannot
        # contain a role, a scope, a table or a column -- and it is the thing
        # actually worth inspecting, because it is what the model produced and
        # what everything downstream was compiled from.
        payload["plan"] = result.plan
    if result.sql:
        payload["sql"] = result.sql
    if result.answer:
        a = result.answer
        payload["answer"] = {
            "headline": a.headline,
            "columns": a.columns,
            "rows": a.table,
            "scope_note": a.scope_note,
            "period_note": a.period_note,
            "warnings": a.warnings,
            "notes": a.notes,
            "row_count": a.row_count,
            "truncated": a.truncated,
        }
    return payload


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
