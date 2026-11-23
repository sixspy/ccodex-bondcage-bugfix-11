"""笼锁与争用登记（公共并发面）。"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit

router = APIRouter(tags=["locks"])


class LockIn(BaseModel):
    lock_key: str
    action: str = "acquire"


def _record_contention(lock_key: str, holder: str, detail: str) -> None:
    """争用痕单独落库：后到请求即使业务整体回退，争用记录也不得被回滚。"""
    with connect() as side:
        side.execute(
            """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
               VALUES (?,?,?,?,?)""",
            (lock_key, holder, "争用", detail, now_iso()),
        )
        side.commit()


def try_acquire(conn, lock_key: str, holder: str) -> bool:
    """抢笼锁。锁获取立即单独提交，使并发连接可见；后到只记争用痕。

    require_lock 在业务写之前调用，故此处 commit 不会夹带未完成业务。
    """
    now = now_iso()
    row = conn.execute(
        "SELECT holder FROM cage_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row is None:
        try:
            conn.execute(
                "INSERT INTO cage_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
                (lock_key, holder, now),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # 竞态：另一路已抢先插入同一锁键
            row = conn.execute(
                "SELECT holder FROM cage_locks WHERE lock_key=?", (lock_key,)
            ).fetchone()
            if row is None or row["holder"] == holder:
                return True
            _record_contention(lock_key, holder, f"持有者={row['holder']}")
            return False
        return True
    if row["holder"] == holder:
        return True
    _record_contention(lock_key, holder, f"持有者={row['holder']}")
    return False


def release(conn, lock_key: str, holder: str) -> None:
    conn.execute(
        "DELETE FROM cage_locks WHERE lock_key=? AND holder=?",
        (lock_key, holder),
    )


def require_lock(conn, lock_key: str, holder: str) -> None:
    if not try_acquire(conn, lock_key, holder):
        raise HTTPException(409, "争用：后到只记争用，不写业务")


@router.get("/api/locks")
def list_locks(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM cage_locks ORDER BY acquired_at DESC"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/contention")
def list_contention(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_log ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/locks/acquire")
def acquire_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        ok = try_acquire(conn, body.lock_key, user)
        write_audit(
            conn,
            user,
            "lock_acquire",
            body.lock_key,
            {"ok": ok},
        )
        conn.commit()
        if not ok:
            raise HTTPException(409, "争用：未能获得锁")
        return {"ok": True, "lock_key": body.lock_key, "holder": user}


@router.post("/api/locks/release")
def release_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        release(conn, body.lock_key, user)
        write_audit(conn, user, "lock_release", body.lock_key, {})
        conn.commit()
        return {"ok": True}
