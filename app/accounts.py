from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .auth import Actor, get_actor, password_change_required, require_admin
from .config import settings
from .database import db, init_db, now_iso, one

router = APIRouter(prefix="/api", tags=["accounts"])
LOGIN_WINDOW = 15 * 60
ACCOUNT_ATTEMPT_LIMIT = 8
IP_ATTEMPT_LIMIT = 40
REGISTRATION_IP_LIMIT = 10
SESSION_SECONDS = 8 * 60 * 60
SECURITY_ACTIONS = (
    "account_created", "account_updated", "login_success", "login_failed", "login_throttled",
    "password_changed", "password_change_failed", "password_reset", "login_unlocked",
    "session_logout", "session_revoked", "sessions_revoked",
)


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,63}$")
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=12, max_length=256)
    role: Literal["admin", "annotator", "reviewer"] = "annotator"

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value):
        return value.lower()

    @field_validator("display_name")
    @classmethod
    def clean_display_name(cls, value):
        if not value.strip():
            raise ValueError("显示名称不能为空")
        return value.strip()


class RegistrationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,63}$")
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=12, max_length=256)
    note: str = Field(default="", max_length=1000)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value):
        return value.lower()

    @field_validator("display_name")
    @classmethod
    def clean_display_name(cls, value):
        if not value.strip():
            raise ValueError("显示名称不能为空")
        return value.strip()

    @field_validator("note")
    @classmethod
    def clean_note(cls, value):
        return value.strip()


class RegistrationReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["approve", "reject"]
    role: Literal["admin", "annotator", "reviewer"] | None = None
    reason: str = Field(default="", max_length=1000)
    expected_revision: int = Field(strict=True, ge=1, le=9223372036854775807)

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value):
        return value.strip()

    @model_validator(mode="after")
    def validate_decision(self):
        if self.action == "approve" and self.role is None:
            raise ValueError("通过申请时请选择账号角色")
        if self.action == "reject" and not self.reason:
            raise ValueError("请填写拒绝原因，便于申请人了解如何修改")
        return self


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    role: Literal["admin", "annotator", "reviewer"] | None = None
    enabled: bool | None = None
    password: str | None = Field(default=None, min_length=12, max_length=256)

    @field_validator("display_name")
    @classmethod
    def clean_display_name(cls, value):
        if value is not None and not value.strip():
            raise ValueError("显示名称不能为空")
        return value.strip() if value is not None else None


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=1, max_length=80)

    @field_validator("display_name")
    @classmethod
    def clean_display_name(cls, value):
        if not value.strip():
            raise ValueError("显示名称不能为空")
        return value.strip()


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


def _scrypt(password: str, salt: str, parallelism: int) -> str:
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8,
                          p=parallelism, maxmem=32 * 1024 * 1024).hex()


def hash_password(password: str, salt: str | None = None) -> str:
    # OWASP's 16 MiB / p=5 profile keeps per-login memory bounded on this server.
    salt = salt or secrets.token_hex(16)
    return f"scrypt-v2${salt}${_scrypt(password, salt, 5)}"


def check_password(password: str, encoded: str) -> bool:
    try:
        algorithm, salt, expected = encoded.split("$")
        if algorithm not in {"scrypt", "scrypt-v2"} or len(bytes.fromhex(salt)) != 16:
            raise ValueError("unsupported password encoding")
        digest = _scrypt(password, salt, 1 if algorithm == "scrypt" else 5)
        if algorithm == "scrypt":
            # Match the work budget of unknown users/current hashes during migration.
            _scrypt(password, salt, 4)
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError):
        _scrypt(password, "0" * 32, 5)
        return False


_DUMMY_PASSWORD_HASH = hash_password("invalid-account-password")


def _request_ip(request: Request | None) -> str:
    # Only the ASGI server's trusted-proxy setup may interpret forwarded addresses.
    return (request.client.host if request and request.client else "unknown")[:128]


def _security_event(conn, actor: str, action: str, user_id: int | None, ip_address="", **details):
    # Explicit non-secret metadata only; never request bodies, passwords or cookies.
    conn.execute("""INSERT INTO audit_logs
        (actor,action,resource_type,resource_id,payload_json,created_at) VALUES (?,?,?,?,?,?)""",
        (actor, action, "user" if user_id else "authentication", str(user_id or ""),
         json.dumps({"ip_address": ip_address, **details}, ensure_ascii=False), now_iso()))


def _audit_actor(actor: Actor) -> str:
    return f"user:{actor.username}" if actor.user_id else actor.id


def _account_row(conn, actor: Actor, *, admin=False, allow_password_change=False):
    """Re-check the acting session inside write transactions, beyond Depends."""
    if actor.user_id is None:
        if admin:
            require_admin(actor)
            return None
        raise HTTPException(409, "此操作需要使用独立账号登录")
    row = conn.execute("""SELECT u.* FROM users u JOIN user_sessions s ON s.user_id=u.id
        WHERE u.id=? AND s.session_id=? AND s.expires_at>? AND u.enabled=1""",
        (actor.user_id, actor.session_id, now_iso())).fetchone()
    if not row:
        raise HTTPException(401, "登录已失效，请重新登录")
    if row["must_change_password"] and not allow_password_change:
        raise password_change_required()
    if admin and row["role"] != "admin":
        raise HTTPException(403, "需要管理员权限")
    return row


def _public_user(row, attempt=None):
    result = {key: row[key] for key in ("id", "username", "display_name", "role", "created_at",
                                       "last_login_at", "password_changed_at")}
    result.update(enabled=bool(row["enabled"]), must_change_password=bool(row["must_change_password"]))
    active = attempt and int(time.time()) - attempt["window_start"] < LOGIN_WINDOW
    result["failed_login_count"] = attempt["failures"] if active else 0
    result["locked_until"] = (datetime.fromtimestamp(attempt["window_start"] + LOGIN_WINDOW, UTC).isoformat()
        if active and attempt["failures"] >= ACCOUNT_ATTEMPT_LIMIT else None)
    return result


def _session_response(user, token_hash: str, session_id: str) -> dict:
    return {"actor": f"user:{user['username']}:{token_hash[:12]}", "role": user["role"],
            "username": user["username"], "display_name": user["display_name"], "user_id": user["id"],
            "must_change_password": bool(user["must_change_password"]), "session_id": session_id}


def _set_session_cookie(response: Response, token: str):
    response.set_cookie("mdp_session", token, httponly=True, secure=settings.cookie_secure,
                        samesite="strict", max_age=SESSION_SECONDS, path="/")


def _delete_sessions(conn, sessions) -> int:
    rows = list(sessions)
    for session in rows:
        owner = f"user:{session['username']}:{session['token_hash'][:12]}"
        conn.execute("UPDATE assets SET lock_owner=NULL,lock_expires_at=NULL WHERE lock_owner=?", (owner,))
        conn.execute("DELETE FROM user_sessions WHERE token_hash=?", (session["token_hash"],))
    return len(rows)


def _user_sessions(conn, user_id: int):
    return conn.execute("""SELECT s.*,u.username FROM user_sessions s JOIN users u ON u.id=s.user_id
        WHERE s.user_id=?""", (user_id,)).fetchall()


def revoke_browser_session(token: str | None, actor_id: str = "logout", ip_address: str = "") -> int:
    """Used by /api/session logout; release only locks belonging to this session."""
    if not token or not token.startswith("account."):
        return 0
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("""SELECT s.*,u.username FROM user_sessions s JOIN users u ON u.id=s.user_id
            WHERE s.token_hash=?""", (token_hash,)).fetchone()
        if not row:
            return 0
        count = _delete_sessions(conn, [row])
        _security_event(conn, f"user:{row['username']}", "session_logout", row["user_id"], ip_address)
        return count


def _new_session(conn, user, request: Request):
    token = "account." + secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    session_id = secrets.token_hex(16)
    now = datetime.now(UTC)
    conn.execute("""INSERT INTO user_sessions
        (token_hash,user_id,expires_at,session_id,created_at,last_seen_at,ip_address,user_agent)
        VALUES (?,?,?,?,?,?,?,?)""", (token_hash, user["id"], (now + timedelta(seconds=SESSION_SECONDS)).isoformat(),
        session_id, now.isoformat(), now.isoformat(), _request_ip(request), request.headers.get("user-agent", "")[:512]))
    return token, _session_response(user, token_hash, session_id)


def _reserve_login_attempt(username: str, ip: str) -> int:
    """Atomically reserve the rate budget before expensive password work."""
    timestamp = int(time.time())
    retry_after = 0
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM login_attempts WHERE window_start<=?", (timestamp - LOGIN_WINDOW,))
        conn.execute("DELETE FROM login_ip_attempts WHERE window_start<=?", (timestamp - LOGIN_WINDOW,))
        account = conn.execute("SELECT * FROM login_attempts WHERE username=?", (username,)).fetchone()
        source = conn.execute("SELECT * FROM login_ip_attempts WHERE ip_address=?", (ip,)).fetchone()
        for attempt, limit in ((account, ACCOUNT_ATTEMPT_LIMIT), (source, IP_ATTEMPT_LIMIT)):
            if attempt and attempt["failures"] >= limit:
                retry_after = max(retry_after, attempt["window_start"] + LOGIN_WINDOW - timestamp)
        if retry_after:
            user = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
            _security_event(conn, "anonymous", "login_throttled", user["id"] if user else None, ip, username=username)
        else:
            conn.execute("""INSERT INTO login_attempts VALUES (?,1,?) ON CONFLICT(username)
                DO UPDATE SET failures=failures+1""", (username, timestamp))
            conn.execute("""INSERT INTO login_ip_attempts VALUES (?,1,?) ON CONFLICT(ip_address)
                DO UPDATE SET failures=failures+1""", (ip, timestamp))
            timestamp = source["window_start"] if source else timestamp
    if retry_after:
        raise HTTPException(429, "登录验证尝试过多，请稍后重试，或联系管理员解锁账号。",
                            headers={"Retry-After": str(max(1, retry_after))})
    return timestamp


def _successful_attempt(conn, username: str, ip: str, window: int):
    conn.execute("DELETE FROM login_attempts WHERE username=?", (username,))
    # Do not erase this IP's failed attempts against other accounts.
    conn.execute("UPDATE login_ip_attempts SET failures=MAX(0,failures-1) WHERE ip_address=? AND window_start=?",
                 (ip, window))


def _public_registration(row, *, applicant=False):
    keys = ("id", "username", "display_name", "status", "reason", "revision",
            "created_at", "updated_at", "reviewed_at")
    result = {key: row[key] for key in keys}
    if applicant:
        result["note"] = row["note"]
        result["can_resubmit"] = row["status"] == "rejected"
    else:
        result.update({key: row[key] for key in ("note", "role", "user_id", "reviewed_by", "ip_address")})
    return result


def _registration_event(conn, actor, action, registration_id, ip, **details):
    conn.execute("""INSERT INTO audit_logs
        (actor,action,resource_type,resource_id,payload_json,created_at) VALUES (?,?,?,?,?,?)""",
        (actor, action, "registration_request", str(registration_id),
         json.dumps({"ip_address": ip, **details}, ensure_ascii=False), now_iso()))


def _reserve_registration_attempt(username: str, ip: str) -> int:
    # Reuse the username/IP credential-verification budget, plus a separate
    # submission budget that successful registrations never clear.
    timestamp = int(time.time())
    retry_after = 0
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM registration_ip_attempts WHERE window_start<=?", (timestamp - LOGIN_WINDOW,))
        attempt = conn.execute("SELECT * FROM registration_ip_attempts WHERE ip_address=?", (ip,)).fetchone()
        if attempt and attempt["attempts"] >= REGISTRATION_IP_LIMIT:
            retry_after = attempt["window_start"] + LOGIN_WINDOW - timestamp
        else:
            conn.execute("""INSERT INTO registration_ip_attempts VALUES (?,1,?) ON CONFLICT(ip_address)
                DO UPDATE SET attempts=attempts+1""", (ip, timestamp))
    if retry_after:
        raise HTTPException(429, "注册申请提交过于频繁，请稍后再试。",
                            headers={"Retry-After": str(max(1, retry_after))})
    return _reserve_login_attempt(username, ip)


@router.post("/register", status_code=202)
def register(payload: RegistrationCreate, request: Request):
    if settings.auth_mode != "accounts":
        raise HTTPException(409, "当前未开放独立账号注册，请联系管理员。")
    username, ip = payload.username, _request_ip(request)
    window = _reserve_registration_attempt(username, ip)
    previous = one("SELECT * FROM registration_requests WHERE username=?", (username,))
    # One scrypt work budget for both new usernames and existing applications.
    encoded = previous["password_hash"] if previous and previous["status"] != "approved" else None
    valid = check_password(payload.password, encoded) if encoded else False
    new_hash = hash_password(payload.password) if not encoded else None
    result = None
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT * FROM registration_requests WHERE username=?", (username,)).fetchone()
        user = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if user or (current and (not previous or not valid or current["status"] == "approved"
                or current["password_hash"] != encoded or current["revision"] != previous["revision"])):
            _security_event(conn, "anonymous", "registration_failed", None, ip, username=username)
        elif current:
            if current["status"] == "rejected":
                conn.execute("""UPDATE registration_requests SET display_name=?,note=?,status='pending',reason='',
                    role=NULL,user_id=NULL,revision=revision+1,ip_address=?,updated_at=?,reviewed_at=NULL,
                    reviewed_by=NULL WHERE id=?""", (payload.display_name, payload.note, ip, now_iso(), current["id"]))
                _registration_event(conn, f"applicant:{username}", "registration_resubmitted", current["id"], ip,
                                    username=username, previous_revision=current["revision"])
                current = conn.execute("SELECT * FROM registration_requests WHERE id=?", (current["id"],)).fetchone()
            # Repeated submissions while pending are idempotent and cannot edit
            # the application currently being read by an administrator.
            _successful_attempt(conn, username, ip, window)
            result = _public_registration(current, applicant=True)
        elif previous is None:
            now = now_iso()
            cursor = conn.execute("""INSERT INTO registration_requests
                (username,display_name,password_hash,note,ip_address,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?)""", (username, payload.display_name, new_hash, payload.note, ip, now, now))
            _registration_event(conn, f"applicant:{username}", "registration_submitted", cursor.lastrowid, ip, username=username)
            _successful_attempt(conn, username, ip, window)
            result = _public_registration(conn.execute("SELECT * FROM registration_requests WHERE id=?", (cursor.lastrowid,)).fetchone(), applicant=True)
    if result is None:
        raise HTTPException(409, "此用户名暂不可用于注册。已有账号请直接登录；重新提交申请请使用原申请密码。")
    return {**result, "message": "申请已提交，请等待管理员审批并分配角色后再登录。"}


@router.get("/registration-requests")
def list_registration_requests(q: str = Query(default="", max_length=100),
                               status: Literal["pending", "approved", "rejected", "all"] = "pending",
                               page: int = Query(default=1, ge=1), page_size: int = Query(default=25, ge=1, le=100),
                               actor: Actor = Depends(get_actor)):
    require_admin(actor)
    clauses, params = [], []
    if status != "all":
        clauses.append("status=?")
        params.append(status)
    if q.strip():
        search = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        clauses.append("(username LIKE ? ESCAPE '\\' OR display_name LIKE ? ESCAPE '\\')")
        params.extend([search, search])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with db() as conn:
        _account_row(conn, actor, admin=True)
        counts = {name: 0 for name in ("pending", "approved", "rejected")}
        for row in conn.execute("SELECT status,COUNT(*) AS n FROM registration_requests GROUP BY status"):
            counts[row["status"]] = row["n"]
        counts["total"] = sum(counts.values())
        total = conn.execute("SELECT COUNT(*) FROM registration_requests" + where, params).fetchone()[0]
        rows = conn.execute("SELECT * FROM registration_requests" + where +
            " ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,updated_at DESC,id DESC LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size]).fetchall()
        return {"items": [_public_registration(row) for row in rows], "total": total,
                "page": page, "page_size": page_size, "counts": counts}


@router.post("/registration-requests/{registration_id}/review")
def review_registration(registration_id: int, payload: RegistrationReview, request: Request,
                        actor: Actor = Depends(get_actor)):
    require_admin(actor)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _account_row(conn, actor, admin=True)
        row = conn.execute("SELECT * FROM registration_requests WHERE id=?", (registration_id,)).fetchone()
        if not row:
            raise HTTPException(404, "注册申请不存在")
        if row["revision"] != payload.expected_revision or row["status"] != "pending":
            raise HTTPException(409, "此申请已被处理或重新提交，请刷新列表后再操作。")
        now, user = now_iso(), None
        if payload.action == "approve":
            if conn.execute("SELECT 1 FROM users WHERE username=?", (row["username"],)).fetchone():
                raise HTTPException(409, "此用户名已存在账号，无法通过该申请，请核实后处理。")
            cursor = conn.execute("""INSERT INTO users
                (username,display_name,password_hash,role,enabled,created_at,must_change_password,password_changed_at)
                VALUES (?,?,?,?,1,?,0,?)""", (row["username"], row["display_name"], row["password_hash"], payload.role, now, now))
            user_id = cursor.lastrowid
            conn.execute("DELETE FROM login_attempts WHERE username=?", (row["username"],))
            conn.execute("""UPDATE registration_requests SET status='approved',reason=?,role=?,user_id=?,
                password_hash='',revision=revision+1,updated_at=?,reviewed_at=?,reviewed_by=? WHERE id=?""",
                (payload.reason, payload.role, user_id, now, now, _audit_actor(actor), registration_id))
            _security_event(conn, _audit_actor(actor), "account_created", user_id, _request_ip(request),
                            username=row["username"], role=payload.role, must_change_password=False)
            user = _public_user(conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())
        else:
            conn.execute("""UPDATE registration_requests SET status='rejected',reason=?,role=NULL,
                revision=revision+1,updated_at=?,reviewed_at=?,reviewed_by=? WHERE id=?""",
                (payload.reason, now, now, _audit_actor(actor), registration_id))
        _registration_event(conn, _audit_actor(actor), "registration_approved" if user else "registration_rejected",
                            registration_id, _request_ip(request), username=row["username"], role=payload.role if user else None,
                            reason=payload.reason, previous_revision=row["revision"], user_id=user["id"] if user else None)
        updated = conn.execute("SELECT * FROM registration_requests WHERE id=?", (registration_id,)).fetchone()
        return {"registration": _public_registration(updated), "user": user}


@router.post("/login")
def login(payload: Login, request: Request, response: Response):
    if settings.auth_mode == "disabled":
        raise HTTPException(409, "当前未启用独立账号登录")
    username = payload.username.strip().lower()
    ip = _request_ip(request)
    window = _reserve_login_attempt(username, ip)
    user = one("SELECT * FROM users WHERE username=?", (username,))
    registration = (one("SELECT * FROM registration_requests WHERE username=? AND status IN ('pending','rejected')", (username,))
                    if not user and settings.auth_mode == "accounts" else None)
    encoded = user["password_hash"] if user else registration["password_hash"] if registration else _DUMMY_PASSWORD_HASH
    valid = check_password(payload.password, encoded)
    upgraded_hash = hash_password(payload.password) if valid and encoded.startswith("scrypt$") else None
    accepted, application_status = None, None
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        current_registration = conn.execute("SELECT * FROM registration_requests WHERE username=? AND status IN ('pending','rejected')", (username,)).fetchone() if registration else None
        if (not current and valid and current_registration and current_registration["password_hash"] == encoded):
            _successful_attempt(conn, username, ip, window)
            application_status = _public_registration(current_registration, applicant=True)
            _registration_event(conn, f"applicant:{username}", "registration_status_checked", current_registration["id"], ip,
                                status=current_registration["status"])
        elif not current or not valid or not current["enabled"] or current["password_hash"] != encoded:
            _security_event(conn, "anonymous", "login_failed", current["id"] if current else None, ip, username=username)
        else:
            _successful_attempt(conn, username, ip, window)
            expired = conn.execute("""SELECT s.*,u.username FROM user_sessions s JOIN users u ON u.id=s.user_id
                WHERE s.expires_at<=?""", (now_iso(),)).fetchall()
            _delete_sessions(conn, expired)
            old_cookie = request.cookies.get("mdp_session", "")
            if old_cookie.startswith("account."):
                old = conn.execute("""SELECT s.*,u.username FROM user_sessions s JOIN users u ON u.id=s.user_id
                    WHERE s.token_hash=?""", (hashlib.sha256(old_cookie.encode()).hexdigest(),)).fetchall()
                _delete_sessions(conn, old)
            conn.execute("UPDATE users SET last_login_at=?,password_hash=? WHERE id=?",
                         (now_iso(), upgraded_hash or encoded, current["id"]))
            accepted = _new_session(conn, current, request)
            _security_event(conn, f"user:{username}", "login_success", current["id"], ip)
    if application_status:
        pending = application_status["status"] == "pending"
        raise HTTPException(403, {"code": "registration_pending" if pending else "registration_rejected",
                            "message": "注册申请正在等待管理员审批，审批通过后即可登录。" if pending else "注册申请未通过。请查看原因后，使用原申请密码修改资料并重新提交。",
                            "registration": application_status})
    if not accepted:
        raise HTTPException(401, "账号或密码不正确，或账号已停用")
    _set_session_cookie(response, accepted[0])
    return accepted[1]


@router.get("/account")
def own_account(actor: Actor = Depends(get_actor)):
    with db() as conn:
        row = _account_row(conn, actor, allow_password_change=True)
        attempts = conn.execute("SELECT * FROM login_attempts WHERE username=?", (row["username"],)).fetchone()
        return _public_user(row, attempts)


@router.patch("/account")
def update_profile(payload: ProfileUpdate, request: Request, actor: Actor = Depends(get_actor)):
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _account_row(conn, actor)
        if row["display_name"] != payload.display_name:
            conn.execute("UPDATE users SET display_name=? WHERE id=?", (payload.display_name, row["id"]))
            _security_event(conn, _audit_actor(actor), "account_updated", row["id"], _request_ip(request), changed_fields=["display_name"])
        updated = conn.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
        attempts = conn.execute("SELECT * FROM login_attempts WHERE username=?", (updated["username"],)).fetchone()
        return _public_user(updated, attempts)


@router.post("/account/password")
def change_password(payload: PasswordChange, request: Request, response: Response, actor: Actor = Depends(get_actor)):
    with db() as conn:
        row = dict(_account_row(conn, actor, allow_password_change=True))
    ip = _request_ip(request)
    window = _reserve_login_attempt(row["username"], ip)
    valid = check_password(payload.current_password, row["password_hash"])
    reused = valid and hmac.compare_digest(payload.current_password.encode(), payload.new_password.encode())
    new_hash = hash_password(payload.new_password) if valid and not reused else None
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = _account_row(conn, actor, allow_password_change=True)
        if current["password_hash"] != row["password_hash"]:
            raise HTTPException(409, "密码已被更新，请重新登录后再修改")
        if not valid:
            _security_event(conn, _audit_actor(actor), "password_change_failed", row["id"], ip)
        elif reused:
            _successful_attempt(conn, row["username"], ip, window)
        else:
            _successful_attempt(conn, row["username"], ip, window)
            revoked = _delete_sessions(conn, _user_sessions(conn, row["id"]))
            conn.execute("UPDATE users SET password_hash=?,must_change_password=0,password_changed_at=? WHERE id=?",
                         (new_hash, now_iso(), row["id"]))
            current = conn.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
            token, result = _new_session(conn, current, request)
            _security_event(conn, _audit_actor(actor), "password_changed", row["id"], ip, revoked_sessions=revoked)
    if not valid:
        raise HTTPException(400, "当前密码不正确，请重新输入。你的登录仍然有效。")
    if reused:
        raise HTTPException(400, "新密码不能与当前密码相同，请设置一个不同的密码。")
    _set_session_cookie(response, token)
    return result


@router.get("/users")
def list_users(request: Request, q: str = Query(default="", max_length=100),
               role: Literal["admin", "annotator", "reviewer"] | None = None,
               enabled: bool | None = None, page: int = Query(default=1, ge=1),
               page_size: int = Query(default=25, ge=1, le=100), actor: Actor = Depends(get_actor)):
    require_admin(actor)
    clauses, params = [], []
    if q.strip():
        search = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        clauses.append("(u.username LIKE ? ESCAPE '\\' OR u.display_name LIKE ? ESCAPE '\\')")
        params.extend([search, search])
    if role is not None:
        clauses.append("u.role=?"); params.append(role)
    if enabled is not None:
        clauses.append("u.enabled=?"); params.append(int(enabled))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    paginated = any(key in request.query_params for key in ("q", "role", "enabled", "page", "page_size"))
    with db() as conn:
        _account_row(conn, actor, admin=True)
        total = conn.execute("SELECT COUNT(*) FROM users u" + where, params).fetchone()[0]
        query = "SELECT u.*,a.failures,a.window_start FROM users u LEFT JOIN login_attempts a ON a.username=u.username" + where + " ORDER BY u.id"
        rows = conn.execute(query + (" LIMIT ? OFFSET ?" if paginated else ""),
                            params + ([page_size, (page - 1) * page_size] if paginated else [])).fetchall()
        users = [_public_user(row, row if row["window_start"] is not None else None) for row in rows]
        if not paginated:
            return users
        admin_count = conn.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND enabled=1").fetchone()[0]
        return {"items": users, "total": total, "page": page, "page_size": page_size, "enabled_admin_count": admin_count}


@router.post("/users", status_code=201)
def create_user(payload: UserCreate, actor: Actor = Depends(get_actor), request: Request = None):
    require_admin(actor)
    encoded = hash_password(payload.password)
    try:
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            _account_row(conn, actor, admin=True)
            if conn.execute("SELECT 1 FROM registration_requests WHERE username=? AND status IN ('pending','rejected')", (payload.username,)).fetchone():
                raise HTTPException(409, "此用户名已有注册申请，请在注册审批中处理，或使用其他用户名。")
            cursor = conn.execute("""INSERT INTO users
                (username,display_name,password_hash,role,created_at,must_change_password)
                VALUES (?,?,?,?,?,1)""", (payload.username, payload.display_name, encoded, payload.role, now_iso()))
            user_id = cursor.lastrowid
            _security_event(conn, _audit_actor(actor), "account_created", user_id, _request_ip(request), username=payload.username, role=payload.role)
            return _public_user(conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "账号名称已存在，请使用其他账号名称") from exc


@router.patch("/users/{user_id}")
def update_user(user_id: int, payload: UserUpdate, request: Request, actor: Actor = Depends(get_actor)):
    require_admin(actor)
    if payload.password and actor.user_id == user_id:
        raise HTTPException(400, "修改自己的密码时，请在账号安全中验证当前密码后再修改。")
    encoded = hash_password(payload.password) if payload.password else None
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _account_row(conn, actor, admin=True)
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "账号不存在")
        role = payload.role or row["role"]
        enabled = row["enabled"] if payload.enabled is None else int(payload.enabled)
        if row["role"] == "admin" and row["enabled"] and (role != "admin" or not enabled):
            if conn.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND enabled=1").fetchone()[0] <= 1:
                raise HTTPException(409, "至少保留一个启用的管理员账号")
        display_name = payload.display_name if payload.display_name is not None else row["display_name"]
        fields = [name for name, value in (("role", role), ("enabled", enabled), ("display_name", display_name)) if row[name] != value]
        conn.execute("""UPDATE users SET role=?,enabled=?,display_name=?,password_hash=?,must_change_password=?,password_changed_at=? WHERE id=?""",
                     (role, enabled, display_name, encoded or row["password_hash"], 1 if encoded else row["must_change_password"],
                      now_iso() if encoded else row["password_changed_at"], user_id))
        if encoded or "role" in fields or "enabled" in fields:
            _delete_sessions(conn, _user_sessions(conn, user_id))
        if encoded:
            conn.execute("DELETE FROM login_attempts WHERE username=?", (row["username"],))
            _security_event(conn, _audit_actor(actor), "password_reset", user_id, _request_ip(request), must_change_password=True)
        if fields:
            _security_event(conn, _audit_actor(actor), "account_updated", user_id, _request_ip(request), changed_fields=fields, role=role, enabled=bool(enabled))
        updated = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        attempts = conn.execute("SELECT * FROM login_attempts WHERE username=?", (updated["username"],)).fetchone()
        return _public_user(updated, attempts)


@router.post("/users/{user_id}/unlock")
def unlock_user(user_id: int, request: Request, actor: Actor = Depends(get_actor)):
    require_admin(actor)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _account_row(conn, actor, admin=True)
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise HTTPException(404, "账号不存在")
        conn.execute("DELETE FROM login_attempts WHERE username=?", (user["username"],))
        _security_event(conn, _audit_actor(actor), "login_unlocked", user_id, _request_ip(request))
        return {"unlocked": True, "user": _public_user(user)}


def _list_sessions(conn, user_id, actor):
    rows = conn.execute("""SELECT session_id,created_at,last_seen_at,expires_at,ip_address,user_agent
        FROM user_sessions WHERE user_id=? AND expires_at>? ORDER BY created_at DESC,session_id""", (user_id, now_iso())).fetchall()
    return {"sessions": [{"id": row["session_id"], "current": row["session_id"] == actor.session_id,
            **{key: row[key] for key in ("created_at", "last_seen_at", "expires_at", "ip_address", "user_agent")}} for row in rows],
            "current_session_id": actor.session_id if actor.user_id == user_id else None}


@router.get("/account/sessions")
def own_sessions(actor: Actor = Depends(get_actor)):
    with db() as conn:
        row = _account_row(conn, actor, allow_password_change=True)
        return _list_sessions(conn, row["id"], actor)


@router.get("/users/{user_id}/sessions")
def user_sessions(user_id: int, actor: Actor = Depends(get_actor)):
    require_admin(actor)
    with db() as conn:
        _account_row(conn, actor, admin=True)
        if not conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone():
            raise HTTPException(404, "账号不存在")
        return _list_sessions(conn, user_id, actor)


def _revoke_sessions(actor, request, response, *, user_id=None, session_id=None, keep_current=False):
    own = user_id is None
    if not own:
        require_admin(actor)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        account = _account_row(conn, actor, admin=not own, allow_password_change=own)
        target_id = account["id"] if own else user_id
        if not conn.execute("SELECT id FROM users WHERE id=?", (target_id,)).fetchone():
            raise HTTPException(404, "账号不存在")
        rows = [row for row in _user_sessions(conn, target_id)
                if (session_id is None or row["session_id"] == session_id)
                and (not keep_current or row["session_id"] != actor.session_id)]
        if session_id and not rows:
            raise HTTPException(404, "此登录会话不存在或已退出")
        current_revoked = any(row["session_id"] == actor.session_id for row in rows)
        count = _delete_sessions(conn, rows)
        _security_event(conn, _audit_actor(actor), "session_revoked" if session_id else "sessions_revoked", target_id,
                        _request_ip(request), revoked_sessions=count)
    if current_revoked:
        response.delete_cookie("mdp_session", path="/", secure=settings.cookie_secure, httponly=True, samesite="strict")
    return {"revoked_count": count, "current_session_revoked": current_revoked}


@router.delete("/account/sessions")
def revoke_own_sessions(request: Request, response: Response, keep_current: bool = True, actor: Actor = Depends(get_actor)):
    return _revoke_sessions(actor, request, response, keep_current=keep_current)


@router.delete("/account/sessions/{session_id}")
def revoke_own_session(session_id: str, request: Request, response: Response, actor: Actor = Depends(get_actor)):
    return _revoke_sessions(actor, request, response, session_id=session_id)


@router.delete("/users/{user_id}/sessions")
def revoke_user_sessions(user_id: int, request: Request, response: Response, actor: Actor = Depends(get_actor)):
    return _revoke_sessions(actor, request, response, user_id=user_id)


def _events(conn, user_id, limit):
    placeholders = ",".join("?" for _ in SECURITY_ACTIONS)
    rows = conn.execute(f"""SELECT id,actor,action,created_at,payload_json FROM audit_logs
        WHERE resource_type='user' AND resource_id=? AND action IN ({placeholders}) ORDER BY id DESC LIMIT ?""",
                        (str(user_id), *SECURITY_ACTIONS, limit)).fetchall()
    events = []
    allowed = {"username", "role", "enabled", "changed_fields", "revoked_sessions", "must_change_password"}
    for row in rows:
        payload = json.loads(row["payload_json"])
        events.append({**{key: row[key] for key in ("id", "actor", "action", "created_at")},
                       "ip_address": payload.get("ip_address", ""),
                       "details": {key: value for key, value in payload.items() if key in allowed}})
    return {"events": events}


@router.get("/account/events")
def own_events(limit: int = Query(default=30, ge=1, le=100), actor: Actor = Depends(get_actor)):
    with db() as conn:
        row = _account_row(conn, actor, allow_password_change=True)
        return _events(conn, row["id"], limit)


@router.get("/users/{user_id}/events")
def user_events(user_id: int, limit: int = Query(default=30, ge=1, le=100), actor: Actor = Depends(get_actor)):
    require_admin(actor)
    with db() as conn:
        _account_row(conn, actor, admin=True)
        if not conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone():
            raise HTTPException(404, "账号不存在")
        return _events(conn, user_id, limit)


if __name__ == "__main__":
    import argparse
    import getpass
    parser = argparse.ArgumentParser(description="创建初始管理员（首次登录后必须修改密码）")
    parser.add_argument("username")
    parser.add_argument("--name", default="管理员")
    args = parser.parse_args()
    password = getpass.getpass("Password (at least 12 characters): ")
    if password != getpass.getpass("Confirm password: "):
        raise SystemExit("Passwords do not match")
    init_db()
    user = create_user(UserCreate(username=args.username, display_name=args.name, password=password, role="admin"), Actor("setup-cli", "admin"))
    print(f"Created administrator: {user['username']} (password change required at first login)")
