"""One-time administrator setup, protected by a machine-local enrollment code."""
from __future__ import annotations

import hmac
import os
import secrets

from fastapi import APIRouter, HTTPException
from pydantic import Field

from .accounts import UserCreate, hash_password
from .config import settings
from .database import db, init_db, now_iso, one

router = APIRouter(prefix="/api/setup", tags=["setup"])


def required() -> bool:
    return settings.auth_mode == "accounts" and one("SELECT id FROM users LIMIT 1") is None


def setup_code() -> str:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    path = settings.data_dir / ".setup-code"
    if not path.exists():
        temporary = path.with_name(f".setup-code-{secrets.token_hex(8)}")
        try:
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
                stream.write(secrets.token_urlsafe(24))
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
    return path.read_text().strip()


class Setup(UserCreate):
    code: str = Field(min_length=16, max_length=128)


@router.get("")
def status():
    return {"required": required()}


@router.post("", status_code=201)
def initialize(payload: Setup):
    if not required():
        raise HTTPException(409, "初始化已完成，请直接登录。")
    if not hmac.compare_digest(payload.code, setup_code()):
        raise HTTPException(403, "初始化口令不正确，请查看启动终端。")
    encoded = hash_password(payload.password)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT id FROM users LIMIT 1").fetchone():
            raise HTTPException(409, "初始化已完成，请直接登录。")
        conn.execute("""INSERT INTO users
            (username,display_name,password_hash,role,created_at,must_change_password)
            VALUES (?,?,?,'admin',?,0)""", (payload.username, payload.display_name, encoded, now_iso()))
    (settings.data_dir / ".setup-code").unlink(missing_ok=True)
    return {"username": payload.username, "initialized": True}


if __name__ == "__main__":
    init_db()
    print(f"初始化口令：{setup_code()}" if required() else "管理员已设置，请直接登录。")
