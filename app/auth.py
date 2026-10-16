from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Cookie, Header, HTTPException, Request

from .config import settings


@dataclass(frozen=True)
class Actor:
    id: str
    role: str
    display_name: str = ""
    user_id: int | None = None
    username: str = ""
    must_change_password: bool = False
    session_id: str | None = None


def password_change_required() -> HTTPException:
    return HTTPException(403, {"code": "password_change_required", "message": "请先修改初始密码，再使用项目功能。"})


def password_change_route_allowed(request: Request) -> bool:
    path = request.url.path.rstrip("/")
    return (
        path == "/api/session" and request.method in {"GET", "DELETE"}
        or path in {"/api/account", "/api/account/events"} and request.method == "GET"
        or path == "/api/account/password" and request.method == "POST"
        or (path == "/api/account/sessions" or path.startswith("/api/account/sessions/"))
        and request.method in {"GET", "DELETE"}
    )


def validate_bootstrap_token(token: str) -> Actor:
    if not settings.bootstrap_api_key:
        raise HTTPException(503, "生产认证已启用，但 MDP_BOOTSTRAP_API_KEY 未配置")
    if not hmac.compare_digest(
        hashlib.sha256(token.encode()).digest(),
        hashlib.sha256(settings.bootstrap_api_key.encode()).digest(),
    ):
        raise HTTPException(401, "访问令牌无效", headers={"WWW-Authenticate": "Bearer"})
    return Actor("bootstrap-admin", "admin")


def create_session_identity() -> str:
    session_id = uuid.uuid4().hex
    issued_at = str(int(time.time()))
    message = f"{session_id}.{issued_at}"
    signature = hmac.new(
        settings.bootstrap_api_key.encode(), message.encode(), hashlib.sha256
    ).hexdigest()
    return f"{message}.{signature}"


def actor_from_session_identity(value: str) -> Actor:
    parts = value.split(".")
    if len(parts) != 3 or len(parts[0]) != 32:
        raise HTTPException(401, "浏览器会话身份无效")
    session_id, issued_at_text, signature = parts
    try:
        issued_at = int(issued_at_text)
    except ValueError as exc:
        raise HTTPException(401, "浏览器会话身份无效") from exc
    if issued_at > int(time.time()) + 60 or int(time.time()) - issued_at > 8 * 60 * 60:
        raise HTTPException(401, "浏览器会话已过期")
    expected = hmac.new(
        settings.bootstrap_api_key.encode(),
        f"{session_id}.{issued_at_text}".encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(401, "浏览器会话身份无效")
    return Actor(f"bootstrap-admin:{session_id[:12]}", "admin")


def get_actor(
    request: Request,
    authorization: str | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    mdp_session: str | None = Cookie(default=None),
) -> Actor:
    """Resolve the audit actor.

    Development mode accepts X-Actor-ID for local tests. Account sessions resolve
    current roles from the database; privileged token access ignores actor headers.
    """
    if settings.auth_mode == "disabled":
        actor_id = (x_actor_id or "local-admin").strip()[:100]
        return Actor(actor_id or "local-admin", "admin")
    if settings.auth_mode not in {"token", "accounts"}:
        raise HTTPException(500, "MDP_AUTH_MODE 配置无效")
    if authorization and authorization.startswith("Bearer "):
        return validate_bootstrap_token(authorization.removeprefix("Bearer ").strip())
    if mdp_session:
        if mdp_session.startswith("account."):
            from .database import one, now_iso
            token_hash = hashlib.sha256(mdp_session.encode()).hexdigest()
            user = one("""SELECT u.*,s.session_id,s.last_seen_at FROM user_sessions s JOIN users u ON u.id = s.user_id
                          WHERE s.token_hash = ? AND s.expires_at > ? AND u.enabled = 1""",
                       (token_hash, now_iso()))
            if not user:
                raise HTTPException(401, "登录已过期或账号已停用，请重新登录")
            if user["must_change_password"] and not password_change_route_allowed(request):
                raise password_change_required()
            # Coalesce activity updates to avoid a write transaction on every API call.
            threshold = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
            if not user["last_seen_at"] or user["last_seen_at"] < threshold:
                from .database import db
                with db() as conn:
                    conn.execute("UPDATE user_sessions SET last_seen_at=? WHERE token_hash=? AND last_seen_at<?",
                                 (now_iso(), token_hash, threshold))
            return Actor(f"user:{user['username']}:{token_hash[:12]}", user["role"], user["display_name"], user["id"],
                         user["username"], bool(user["must_change_password"]), user["session_id"])
        if not settings.bootstrap_api_key:
            raise HTTPException(401, "登录会话无效")
        return actor_from_session_identity(mdp_session)
    else:
        message = "请使用独立账号登录" if settings.auth_mode == "accounts" else "需要 Bearer Token"
        raise HTTPException(401, message, headers={"WWW-Authenticate": "Bearer"})


def require_reviewer(actor: Actor) -> None:
    if actor.must_change_password:
        raise password_change_required()
    if actor.role not in {"reviewer", "admin"}:
        raise HTTPException(403, "需要审核员权限")


def require_admin(actor: Actor) -> None:
    if actor.must_change_password:
        raise password_change_required()
    if actor.role != "admin":
        raise HTTPException(403, "需要管理员权限")
