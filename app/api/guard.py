"""Request-level protections that apply before any route runs.

Three things, in this order, for every state-changing API request:

1. **Same origin.** Authentication is a cookie, and a browser attaches a
   cookie to a request another site makes it send. SameSite=Lax stops a
   cross-site POST from carrying it in current browsers; this is the second
   control, independent of browser behaviour. A request whose ``Origin`` (or,
   failing that, ``Sec-Fetch-Site``) says another site sent it is refused.
   Login is included -- login CSRF signs a victim into the attacker's
   account. Non-browser clients send neither header and are not affected:
   CSRF is an attack on ambient browser credentials.
2. **JSON only.** A cross-origin request with a JSON content type needs a
   CORS preflight, which this API never grants, so a form or a simple
   request cannot reach a route at all.
3. **Size.** A body larger than the limit is refused with 413 while it is
   being read -- not only when it declares its length -- so a chunked upload
   cannot hold a worker reading megabytes the routes would reject anyway.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


async def _refuse(send, status: int, code: str, message: str) -> None:
    body = json.dumps({"detail": {"code": code, "message": message}}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class RequestGuard:
    def __init__(self, app, *, max_body_bytes: int, allowed_origins: set[str]):
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.allowed_origins = {o.rstrip("/").lower() for o in allowed_origins if o}

    def _same_origin(self, origin: str, host: str | None) -> bool:
        origin = origin.rstrip("/").lower()
        if origin in self.allowed_origins:
            return True
        # Compared on host[:port]: the session cookie is Secure, so an
        # http:// page on the same host cannot carry it, and behind a TLS
        # proxy the scheme the app sees depends on proxy trust configuration.
        return bool(host) and urlsplit(origin).netloc == host.lower()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in UNSAFE \
                or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)

        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}

        origin = headers.get("origin")
        fetch_site = headers.get("sec-fetch-site")
        if origin is not None and origin != "null":
            if not self._same_origin(origin, headers.get("host")):
                return await _refuse(send, 403, "cross_origin",
                                     "Requests from another site are not accepted.")
        elif origin == "null" or (fetch_site is not None
                                  and fetch_site not in ("same-origin", "none")):
            return await _refuse(send, 403, "cross_origin",
                                 "Requests from another site are not accepted.")

        declared = headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_body_bytes:
                    return await _refuse(send, 413, "request_too_large",
                                         "That request is too large.")
            except ValueError:
                return await _refuse(send, 400, "bad_request", "Invalid Content-Length.")
        has_body = (declared not in (None, "0")) or "chunked" in headers.get(
            "transfer-encoding", "").lower()
        media_type = headers.get("content-type", "").split(";")[0].strip().lower()
        if has_body and media_type != "application/json":
            return await _refuse(send, 415, "unsupported_media_type",
                                 "Requests must be sent as application/json.")

        seen = 0
        limit = self.max_body_bytes
        state = {"started": False, "refused": False}

        async def guarded_send(message):
            # Once the guard has answered 413, nothing the app sends after it
            # reaches the client.
            if state["refused"]:
                return
            if message["type"] == "http.response.start":
                state["started"] = True
            await send(message)

        async def counted():
            nonlocal seen
            if state["refused"]:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    # Answered here rather than by raising: FastAPI turns an
                    # error during body reading into a 400, which refuses the
                    # request but misstates why. The app is told the client
                    # went away, so it stops reading.
                    if not state["started"]:
                        await _refuse(send, 413, "request_too_large",
                                      "That request is too large.")
                    state["refused"] = True
                    return {"type": "http.disconnect"}
            return message

        await self.app(scope, counted, guarded_send)
