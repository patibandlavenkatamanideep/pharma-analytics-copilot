"""HTTP surface.

The API exposes questions, not queries. There is deliberately no endpoint that
accepts SQL, a role, a scope, a user id or a metric formula: every one of those
is derived server-side from a verified session.

SQL and technical detail are hidden by default. A user may ask to see the SQL,
and then they see only the statement their own principal was authorized to run.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Any

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from psycopg_pool import PoolTimeout
from pydantic import BaseModel, Field

from app import admission, logs, telemetry
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
from app.conversation import feedback, privacy, runs
from app.pipeline import Pipeline, to_payload

log = logging.getLogger(__name__)

_pipeline: Pipeline | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # First, so nothing after it is written unsanitised (app/logs.py).
    logs.configure(settings)
    if not _frontend["mounted"]:
        log.warning("web/dist not built; API only")

    # Refuse to serve if the database boundary is not what we think it is. A
    # mis-provisioned deployment fails loudly instead of quietly serving
    # unrestricted data. Each refusal is logged with a stable reason first:
    # sanitised logs carry no exception text, and an operator still needs
    # to know why the process would not start.
    try:
        problems = verify_runtime_role_safety()
    except DATABASE_UNAVAILABLE:
        log.critical("startup.refused", extra={"reason": "database_unreachable"}, exc_info=True)
        raise
    if problems:
        log.critical("startup.refused", extra={"reason": "security_boundary_broken"})
        raise RuntimeError("database security boundary is not intact: " + "; ".join(problems))

    # The serving process does not need the owner credential: it never
    # creates, alters or loads anything. Holding it anyway turns a compromise
    # of the web process into a compromise of the schema and every table.
    # In the cloud environment that is refused; locally (tests and scripts
    # share one .env) it is a warning.
    for problem in serving_credential_problems(settings):
        if settings.environment == "cloud":
            log.critical("startup.refused", extra={"reason": "owner_credential_present"})
            raise RuntimeError(problem)
        log.warning("serving with %s (allowed only because PAC_ENVIRONMENT=local)",
                    "owner_credential_present")

    # Exports nothing unless a collector is configured; never fatal.
    telemetry.configure(settings)

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
    telemetry.shutdown()
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

#: docs/API.md describes this version. Bumped when a response shape changes.
API_VERSION = "2"


OVERLOADED_MESSAGE = ("The service is busy right now. Your question was not "
                      "answered; please try again in a moment.")


def _overloaded(retry_after: int) -> HTTPException:
    return HTTPException(status_code=503, detail={
        "code": "overloaded", "message": OVERLOADED_MESSAGE},
        headers={"Retry-After": str(retry_after)})


_in_flight = {"asks": 0}


@app.middleware("http")
async def _admit_requests(request: Request, call_next):
    """At most admission_max_inflight_requests questions in flight in this
    worker. Beyond that, 503 at once -- before the body is read -- rather
    than an unbounded queue in the thread pool (app/admission.py). Counted in
    the event loop, which is single-threaded, so a plain counter is exact."""
    if request.method != "POST" or request.url.path != "/api/ask":
        return await call_next(request)
    if _in_flight["asks"] >= get_settings().admission_max_inflight_requests:
        telemetry.count("pac.admission.refused", stage="request", reason="inflight_limit")
        return JSONResponse(status_code=503, headers={"Retry-After": "2"}, content={
            "detail": {"code": "overloaded", "message": OVERLOADED_MESSAGE}})
    _in_flight["asks"] += 1
    try:
        return await call_next(request)
    finally:
        _in_flight["asks"] -= 1


@app.middleware("http")
async def _api_version(request: Request, call_next):
    # Outermost, so refusals by the request guard are measured too. Labelled
    # by route TEMPLATE (/api/runs/{run_id}), never by the path itself.
    started = time.perf_counter()
    code = 500
    template = "unmatched"
    try:
        with telemetry.span("http.server", **{"http.request.method": request.method}) as span:
            response = await call_next(request)
            code = response.status_code
            template = getattr(request.scope.get("route"), "path", None) or "unmatched"
            span.set(**{"http.route": template, "http.response.status_code": code})
    finally:
        telemetry.count("pac.http.server.requests", **{
            "http.route": template, "http.request.method": request.method,
            "http.response.status_class": telemetry.status_class(code)})
        telemetry.observe("pac.http.server.duration", (time.perf_counter() - started) * 1000,
                          **{"http.route": template})
    if request.url.path.startswith("/api/"):
        response.headers["X-API-Version"] = API_VERSION
    return response


@app.middleware("http")
async def _correlate(request: Request, call_next):
    """Outermost: every log line written while serving this request carries
    its id, and the response returns it as X-Request-ID, so a user's report
    and an operator's log meet. Generated here -- a client cannot choose it,
    so it cannot be made to collide with anyone else's."""
    http_id = uuid.uuid4().hex[:16]
    token = logs.bind(http_id=http_id)
    try:
        response = await call_next(request)
    finally:
        logs.unbind(token)
    response.headers["X-Request-ID"] = http_id
    return response


#: Failures of the database itself -- unreachable, or no pooled connection
#: within the pool timeout. Transient: the answer is "try again", not "this
#: request is wrong", so 503 with Retry-After rather than 500. Statement
#: timeouts are not here; the pipeline answers those itself.
DATABASE_UNAVAILABLE = (psycopg.OperationalError, PoolTimeout)


@app.exception_handler(psycopg.OperationalError)
@app.exception_handler(PoolTimeout)
async def _database_unavailable(request: Request, exc: Exception) -> JSONResponse:
    log.warning("database unavailable on %s: %s", request.url.path, type(exc).__name__)
    telemetry.count("pac.db.errors", kind="unavailable")
    return JSONResponse(status_code=503, headers={"Retry-After": "5"}, content={
        "detail": {"code": "database_unavailable",
                   "message": "The service is temporarily unavailable. Please try again."}})


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
    with telemetry.span("pac.auth", expected=(HTTPException,)) as span:
        principal, due = identity.resolve_session(token)
        if principal is None:
            span.set(**{"pac.auth.outcome": "absent" if not token else "rejected"})
            raise HTTPException(status_code=401, detail="Not signed in.")
        outcome = "ok"
        if due and token:
            rotated = identity.rotate(token)
            if rotated is not None:
                _set_session_cookie(response, *rotated)
                outcome = "rotated"
        span.set(**{"pac.auth.outcome": outcome, "pac.role": principal.role})
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


# ---------------------------------------------------------------------------
# Single sign-on (OpenID Connect) -- off unless configured
# ---------------------------------------------------------------------------

_oidc = None


def oidc_provider():
    """The configured provider, or 404 when SSO is not enabled: an endpoint
    that exists but cannot work is a puzzle for an operator."""
    from app.auth.oidc import Provider

    global _oidc
    if not get_settings().oidc_enabled:
        raise HTTPException(status_code=404, detail="Single sign-on is not enabled.")
    if _oidc is None:
        _oidc = Provider()
    return _oidc


@app.get("/api/auth/methods")
def auth_methods() -> dict[str, Any]:
    """Which sign-in methods the sign-in page should offer. Public."""
    return {"password": True, "oidc": get_settings().oidc_enabled}


@app.get("/api/auth/oidc/start")
def oidc_start(request: Request, next: str = "/") -> RedirectResponse:
    from app.auth.oidc import PENDING_TTL_SECONDS, binding_cookie_name

    settings = get_settings()
    name = binding_cookie_name(settings.cookie_secure)
    begun = oidc_provider().begin(next, browser=request.cookies.get(name))
    response = RedirectResponse(begun.authorization_url, status_code=302)
    # The browser binding (app/auth/oidc.py). Lax, not Strict: the callback
    # is a top-level GET from the provider's site, and a Strict cookie would
    # not be sent on it. Lives exactly as long as the attempt it binds.
    response.set_cookie(key=name, value=begun.binding, max_age=PENDING_TTL_SECONDS,
                        path="/", httponly=True, secure=settings.cookie_secure,
                        samesite="lax")
    return response


@app.get("/api/auth/oidc/callback")
def oidc_callback(request: Request, code: str = "", state: str = "",
                  error: str | None = None) -> Response:
    from app.auth.identity import _hash_ip
    from app.auth.oidc import OIDCError, binding_cookie_name

    provider = oidc_provider()
    settings = get_settings()
    name = binding_cookie_name(settings.cookie_secure)
    browser = request.cookies.get(name)
    if error:
        # The provider declined (the user cancelled, or policy refused). Its
        # own description is not repeated: it can name internal policy. The
        # attempt is over -- for the browser that started it; another browser
        # cannot end someone else's.
        provider.cancel(state=state, browser=browser)
        raise HTTPException(status_code=400, detail={
            "code": "provider_declined", "message": "Sign-in was not completed."})
    try:
        token, expires_at, _principal, target = provider.complete(
            code=code, state=state, browser=browser,
            user_agent=request.headers.get("user-agent"),
            ip_hash=_hash_ip(request.client.host if request.client else None))
    except OIDCError as exc:
        status = 403 if exc.code in ("not_linked", "disabled", "no_scope") else 400
        raise HTTPException(status_code=status,
                            detail={"code": exc.code, "message": str(exc)}) from None
    response = RedirectResponse(target, status_code=303)
    _set_session_cookie(response, token, expires_at)
    if not provider.pending_in(browser):
        # Nothing else from this browser is in flight: the binding is spent.
        response.delete_cookie(name, path="/", secure=settings.cookie_secure,
                               httponly=True, samesite="lax")
    return response


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
            # Freshness: the latest sale the data contains, when this
            # generation was published, and when an incremental feed last
            # delivered (changed or not) -- so "as of" is never a guess.
            "data_through": dataset["reporting_anchor"].get("max_txn"),
            "published_at": _iso(dataset["published_at"]),
            "incremental": dataset["parent_dataset_id"] is not None,
            "last_ingest_at": _iso(dataset["last_ingest_at"]),
        },
    }


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


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
    except admission.Overloaded as exc:
        raise _overloaded(exc.retry_after) from None
    except DATABASE_UNAVAILABLE:
        raise                          # 503, by the handler below
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


class FeedbackRequest(BaseModel):
    run_id: str = Field(max_length=64)
    helpful: bool
    reason: str | None = Field(default=None, max_length=40)
    comment: str | None = Field(default=None, max_length=500)


@app.post("/api/feedback")
def give_feedback(body: FeedbackRequest, user: CurrentUser) -> dict[str, Any]:
    """Rate one of your own answers. Replaces earlier feedback on it."""
    try:
        recorded = feedback.record(user, body.run_id, helpful=body.helpful,
                                   reason=body.reason, comment=body.comment)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_reason",
                                                     "message": str(exc)}) from None
    if not recorded:
        raise HTTPException(status_code=404, detail="That answer does not exist.")
    return {"recorded": True}


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


@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, user: CurrentUser) -> dict[str, Any]:
    """Delete one of your conversations and everything hanging off it."""
    try:
        deleted = privacy.delete_conversation(user, conversation_id,
                                              pipeline().graph.checkpointer)
    except privacy.ConversationBusy as exc:
        raise HTTPException(status_code=409, detail={
            "code": "conversation_busy", "message": str(exc)}) from None
    if not deleted:
        raise HTTPException(status_code=404, detail="That conversation does not exist.")
    return {"deleted": True}


@app.get("/api/me/data")
def export_my_data(user: CurrentUser) -> JSONResponse:
    """Your data, as you may currently see it, as a download."""
    from fastapi.encoders import jsonable_encoder

    return JSONResponse(jsonable_encoder(privacy.export(user)), headers={
        "Content-Disposition": 'attachment; filename="my-data.json"',
        "Cache-Control": "no-store"})


@app.delete("/api/me/data")
def delete_my_data(user: CurrentUser) -> dict[str, Any]:
    """Delete every conversation you own. You stay signed in; audit records
    are kept (see docs/RETENTION.md)."""
    try:
        done = privacy.delete_all(user, pipeline().graph.checkpointer)
    except privacy.ConversationBusy as exc:
        raise HTTPException(status_code=409, detail={
            "code": "conversation_busy", "message": str(exc)}) from None
    return {"deleted": {"conversations": done.conversations, "workflow_threads": done.threads}}


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict[str, str]:
    """Liveness: the process answers. Also which release it is running."""
    return {"status": "ok", "release": get_settings().release}


@app.get("/ready")
def ready() -> JSONResponse:
    """Ready only when a published dataset exists and the boundary is intact.

    Unauthenticated, so the reason is a fixed phrase, never the exception:
    a database error names the login role, the host and why authentication
    failed, and this endpoint used to return it to anyone who asked.
    """
    try:
        dataset = pipeline().current_dataset()
        # Every question writes workflow checkpoints; without the store no
        # question can be answered. The model provider and the telemetry
        # collector are deliberately NOT checked: losing either degrades
        # answers or observability, and taking every replica out of service
        # for it would turn a partial outage into a total one.
        from app.db import graph_pool

        with graph_pool().connection() as conn:
            conn.execute("SELECT 1 FROM checkpoint_migrations LIMIT 1")
    except DATABASE_UNAVAILABLE as exc:
        log.warning("not ready: database unavailable (%s)", type(exc).__name__)
        return JSONResponse({"status": "not ready", "reason": "database unavailable"},
                            status_code=503)
    except Exception as exc:
        log.warning("not ready: %s", exc)
        return JSONResponse({"status": "not ready", "reason": "no published dataset"},
                            status_code=503)
    return JSONResponse({"status": "ready", "dataset": dataset["dataset_id"]})


@app.exception_handler(ConversationAccessError)
def _conversation_denied(request: Request, exc: ConversationAccessError) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=status.HTTP_404_NOT_FOUND)


_frontend = {"mounted": False}


def mount_frontend() -> None:
    """Serve the built UI from the same origin, so the cookie needs no CORS.
    Its absence is reported at startup, once logging is configured -- not
    here, at import, where it would be printed unformatted."""
    import pathlib

    dist = pathlib.Path(__file__).resolve().parent.parent.parent / "web" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="web")
        _frontend["mounted"] = True


mount_frontend()
