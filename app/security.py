"""Browser request protections shared by all account and business endpoints."""
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse

from .config import settings


async def request_budget(scope):
    """Authenticate uploads before FastAPI parses their multipart bodies."""
    import re
    from starlette.concurrency import run_in_threadpool
    from .auth import get_actor, require_admin
    path = scope.get("path", "").rstrip("/")
    checkpoint = path == "/api/model-downloads/import/checkpoint"
    images = re.fullmatch(r"/api/projects/[^/]+/assets", path) is not None
    if scope.get("method") == "POST" and (checkpoint or images):
        request = Request(scope)
        actor = await run_in_threadpool(
            get_actor, request, request.headers.get("authorization"),
            request.headers.get("x-actor-id"), request.cookies.get("mdp_session"),
        )
        if checkpoint:
            require_admin(actor)
        # One MiB allows multipart boundaries/headers; handlers enforce file limits.
        return ((2048 if checkpoint else settings.max_upload_mb) + 1) * 1024 * 1024
    return settings.max_request_mb * 1024 * 1024


def _origin(value: str) -> str | None:
    try:
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            return None
        port = url.port if url.port is not None else (443 if url.scheme == "https" else 80)
        return f"{url.scheme}://{url.hostname.lower()}:{port}"
    except ValueError:
        return None


async def browser_security(request: Request, call_next):
    path = request.scope["path"]
    unsafe = request.method not in {"GET", "HEAD", "OPTIONS"}
    bearer = request.headers.get("authorization", "").startswith("Bearer ")
    protected = settings.auth_mode != "disabled" and path.startswith("/api/") and unsafe and (
        request.cookies.get("mdp_session") or path in {"/api/login", "/api/register", "/api/session", "/api/setup"}
    ) and not bearer
    if protected:
        target = _origin(settings.public_origin or str(request.base_url))
        source = request.headers.get("origin")
        if source is None:
            source = request.headers.get("referer")
        # A custom non-simple header supports non-browser clients and browsers which
        # omit Origin. CORS is deliberately not enabled; other sites cannot set it.
        valid = target is not None and (_origin(source) == target if source is not None else (
            request.headers.get("x-mdp-client") == "browser"
            and request.headers.get("sec-fetch-site") not in {"cross-site", "same-site"}
        ))
        if not valid:
            response = JSONResponse(status_code=403, content={"detail": "请求来源验证失败，请从本站重新打开页面后再操作。"})
        else:
            response = await call_next(request)
    else:
        response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
    if path.startswith(("/api/login", "/api/register", "/api/registration-requests", "/api/session", "/api/account", "/api/users", "/api/setup")):
        response.headers["Cache-Control"] = "no-store"
    return response
