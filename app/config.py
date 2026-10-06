"""Application settings. Every secret is read from the environment; nothing
sensitive is ever written to a file in this repository."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAC_", env_file=".env", extra="ignore")

    # --- database -----------------------------------------------------------
    # Three separate login roles. A scoped connection has no membership path to
    # the Exec role, so it cannot SET ROLE its way to pricing data.
    db_host: str = "127.0.0.1"
    db_port: int = 5432
    db_name: str = "pharma_analytics"

    db_owner_user: str = "pac_owner"
    db_owner_password: str = ""

    db_auth_user: str = "pac_auth_login"
    db_auth_password: str = ""

    db_exec_user: str = "pac_exec_login"
    db_exec_password: str = ""

    db_scoped_user: str = "pac_scoped_login"
    db_scoped_password: str = ""

    # --- execution limits ---------------------------------------------------
    statement_timeout_ms: int = 5_000
    max_result_rows: int = 5_000

    # --- domain ---------------------------------------------------------------
    # The company's own name(s), as users say them: "NovaPharma volume" is
    # company volume, not a product called NovaPharma. It comes from the
    # business documents, not from the data -- products carry no manufacturer
    # -- so it is configuration, and a different client sets their own.
    # Comma-separated in the environment: PAC_COMPANY_NAMES="NovaPharma,Nova".
    company_names: str = "NovaPharma"

    # --- incremental ingestion --------------------------------------------------
    # The timezone a source timestamp is converted in to decide which
    # business day -- and so which week and month -- a sale belongs to. A sale
    # at 23:30 New York time on a Saturday is Saturday's, whatever UTC says.
    business_timezone: str = "America/New_York"
    # A batch in which more than this share of events fail validation is
    # rejected whole: past that point the feed is broken, and publishing the
    # remainder would present a partial period as a complete one.
    ingest_max_quarantine_ratio: float = 0.05
    # How long a publication waits before reclaiming the rows it replaced:
    # longer than any of this application's read transactions can run
    # (statement timeout 5 s, idle-in-transaction 10 s). A VACUUM run
    # earlier reclaims nothing that an in-flight reader can still see.
    publication_settle_seconds: float = 20.0

    # --- retention (app/retention.py; proposals, not an agreed policy) -----------
    # A conversation untouched this long is deleted with its turns, cohorts,
    # clarifications, runs and workflow state.
    conversation_retention_days: int = 180
    # The security audit trail: kept this long, then deleted by the jobs
    # container. Not shortened by a user's deletion request.
    audit_retention_days: int = 400
    # Ended sessions and failed sign-in attempts are evidence for abuse
    # investigation for a while, then noise.
    session_record_retention_days: int = 30
    login_attempt_retention_days: int = 30
    # Quarantined ingestion events, kept for diagnosis and resubmission.
    quarantine_retention_days: int = 90

    # --- telemetry ----------------------------------------------------------------
    # The release this process is running, reported with every span. Set by
    # the build to the image's source revision; "dev" means unreleased.
    release: str = "dev"
    # An OTLP/HTTP collector's base URL (http://collector:4318). Unset, no
    # telemetry is exported and recording is a no-op.
    otel_endpoint: str = ""
    # Bounds every export call; a slow collector costs at most this, in the
    # background, never on a request.
    otel_timeout_s: float = 5.0
    # Comma-separated operator inventory; at most 64 source metric series.
    otel_source_names: str = "synthetic-distributor"
    # Logs: one sanitised JSON object per line (app/logs.py). "text" is
    # Python's default formatting, for local work only: it prints exception
    # text and full request paths.
    log_format: Literal["json", "text"] = "json"
    log_level: str = "INFO"
    # The contracted model rates, USD per million tokens. Unset, no cost is
    # estimated -- a made-up price would read as a measurement.
    llm_input_usd_per_mtok: float | None = None
    llm_output_usd_per_mtok: float | None = None

    # --- runs -------------------------------------------------------------------
    # How long one request may hold a conversation before another may take
    # it over. Longer than the slowest legitimate request, or a slow answer
    # loses its lease mid-flight and is refused at commit as a conflict.
    run_lease_seconds: int = 120
    # The whole request's wall-clock budget: planning (every attempt and SDK
    # retry), execution and commit. Shorter than the lease, so a run that
    # respects it never loses its lease mid-flight.
    request_deadline_seconds: int = 60
    # How long a client idempotency key keeps its meaning.
    idempotency_retention_seconds: int = 86_400
    # Per user, across every worker and replica (counted in the database).
    user_requests_per_minute: int = 20
    user_requests_per_hour: int = 300
    user_concurrent_runs: int = 2
    # Admission (app/admission.py), per worker process. Sized from the load
    # profile: throughput peaks near 8 concurrent analytical queries per
    # replica (2 workers x 4), and expensive queries start timing out well
    # before 32. Questions beyond these limits get 503 `overloaded` at once
    # rather than a timeout later.
    admission_max_inflight_requests: int = 24
    admission_query_slots: int = 4
    admission_query_queue: int = 16
    admission_query_wait_seconds: float = 10.0
    max_result_bytes: int = 4_000_000

    # --- LLM ----------------------------------------------------------------
    # "bedrock" uses AWS Bedrock; "offline" uses the deterministic rule-based
    # planner so that every non-LLM layer stays testable without credentials.
    llm_provider: Literal["bedrock", "offline"] = "offline"
    bedrock_region: str = "us-east-1"
    # The model the deployment actually runs, and the one the recorded
    # evaluation numbers were measured on. The previous default named a model
    # that is not enabled on this account, so an operator who set
    # PAC_LLM_PROVIDER=bedrock without also setting the model id got a 403
    # naming a model they had never asked for.
    #
    # Dated Anthropic models are invoked through a cross-region inference
    # profile, hence the "us." prefix.
    bedrock_model_id: str = "us.anthropic.claude-opus-4-5-20251101-v1:0"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    llm_max_tokens: int = 4_096
    llm_timeout_s: float = 30.0

    # --- sessions -----------------------------------------------------------
    # Absolute: fixed at sign-in, never extended by activity or rotation.
    session_ttl_hours: int = 12
    # Idle: a session unused this long is dead inside its absolute window.
    session_idle_minutes: int = 30
    # Rotation: a token older than this is replaced on its next use, so a
    # leaked token is useful for minutes rather than for the whole day.
    session_rotate_minutes: int = 15
    # How long a rotated-out token keeps working, so requests already in
    # flight with the old cookie are not signed out mid-answer.
    session_rotation_grace_seconds: int = 30

    # --- request protections ----------------------------------------------------
    # Origins allowed to send state-changing requests besides the app's own.
    # Same-origin needs no entry. Comma-separated.
    allowed_origins: str = ""
    # Largest request body accepted, in bytes. A question is at most 1,000
    # characters; nothing the API accepts comes close to this.
    max_request_bytes: int = 65_536

    # --- single sign-on (OpenID Connect) -----------------------------------------
    # Off unless configured. Local password sign-in is unaffected either way.
    oidc_enabled: bool = False
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    # Empty for a public client (PKCE only); set for a confidential one.
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = ""
    oidc_scopes: str = "openid email profile"
    # Asymmetric only: an HMAC algorithm would let anyone holding the public
    # key forge tokens, and "none" is never acceptable.
    oidc_algorithms: str = "RS256,ES256"
    # Off by default. When on, the FIRST sign-in of an unlinked identity is
    # linked by a provider-verified email; after that (issuer, subject) is the
    # key and email plays no part.
    oidc_link_by_verified_email: bool = False
    cookie_secure: bool = True
    cookie_name: str = "pac_session"

    # --- versions (stamped onto every audit row) ----------------------------
    schema_version: str = "1.0.0"
    policy_version: str = "1.0.0"

    environment: Literal["local", "cloud"] = "local"

    def dsn(self, role: Literal["owner", "auth", "exec", "scoped"]) -> str:
        """A connection string for one runtime role.

        Built with psycopg's own ``make_conninfo`` rather than by
        interpolating into a URL. A generated password containing ``@``,
        ``:``, ``/``, ``?`` or ``#`` broke the URL form: ``@`` split the
        authority so the host became part of the password, and the client
        then tried to reach a host that did not exist -- or, worse,
        one that did.

        make_conninfo emits the keyword/value form and quotes each value, so
        no character in a credential can change the meaning of the string.
        """
        from psycopg.conninfo import make_conninfo

        user, password = {
            "owner": (self.db_owner_user, self.db_owner_password),
            "auth": (self.db_auth_user, self.db_auth_password),
            "exec": (self.db_exec_user, self.db_exec_password),
            "scoped": (self.db_scoped_user, self.db_scoped_password),
        }[role]
        return make_conninfo(
            host=self.db_host,
            port=self.db_port,
            dbname=self.db_name,
            user=user,
            # Omitted entirely when empty, so peer/trust authentication still
            # works rather than being sent an empty password.
            **({"password": password} if password else {}),
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
