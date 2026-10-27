"""Shared business boundary for machine API and MCP; no transport-specific business rules."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import func, select, text

from .database import AICredential, AIJob, AIRequest, AISecret, AuditEvent

SCOPES = {
    "status:read": ("查看总体状态和异常事件", "dashboard", False),
    "checks:read": ("查看全部巡检项及检查结果", "inspection", False),
    "history:read": ("查看历史趋势", "history", False),
    "tasks:read": ("查看自己提交的 AI 任务、进度和结果", "tasks", False),
    "inspection:run": ("执行只读巡检（管理员，可能产生配置中已有的通知）", "inspection", True),
    "checks:write": ("启用或停用单个巡检项（管理员，停用会关闭其当前异常）", "inspection", True),
}


def now() -> int:
    return int(time.time())


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def iso(value: int | None) -> str | None:
    return datetime.fromtimestamp(value, UTC).isoformat() if value else None


@dataclass
class Principal:
    username: str
    credential_id: str
    scopes: set[str]


class AIService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.db = runtime.database
        self.workers: dict[str, asyncio.Task] = {}

    def audit(self, actor: str, operation: str, target: str, result: str):
        with self.db.session() as session:
            session.add(
                AuditEvent(
                    actor=actor, operation=operation, target=target[:200], result=result, at=now()
                )
            )

    def identity(self, username: str) -> dict:
        user = self.runtime.users.identity(username)
        if not user:
            raise HTTPException(401, "用户已停用或删除")
        return user

    def allowed_scopes(self, username: str) -> set[str]:
        user = self.identity(username)
        return {
            key
            for key, (_, page, admin) in SCOPES.items()
            if (not admin or user["role"] == "admin")
            and (page in user["pages"] or user["role"] == "admin")
        }

    def require(self, principal: Principal, scope: str):
        if scope not in principal.scopes & self.allowed_scopes(principal.username):
            raise HTTPException(403, f"缺少权限: {scope}")

    def metadata(self, row: AICredential) -> dict:
        return {
            "id": row.id,
            "name": row.name,
            "prefix": row.prefix,
            "scopes": row.scopes,
            "created_at": iso(row.created_at),
            "expires_at": iso(row.expires_at),
            "status": "revoked"
            if row.revoked
            else "expired"
            if row.expires_at <= now()
            else "active",
        }

    def create_credential(
        self, username: str, name: str, scopes: list[str], expires_at: int
    ) -> dict:
        if not scopes or not set(scopes) <= self.allowed_scopes(username):
            raise HTTPException(403, "所选权限超出当前用户权限")
        if not now() < expires_at <= now() + 366 * 86400:
            raise HTTPException(422, "有效期须在未来 366 天内")
        secret = "psi_" + secrets.token_urlsafe(32)
        row = AICredential(
            id=uuid.uuid4().hex,
            username=username,
            name=name,
            digest=digest(secret),
            prefix=secret[:12],
            scopes=sorted(set(scopes)),
            created_at=now(),
            expires_at=expires_at,
            revoked=False,
        )
        with self.db.session() as session:
            session.add(row)
        self.audit(username, "credential.create", row.id, "success")
        return {**self.metadata(row), "secret": secret}

    def credentials(self, username: str, offset: int, limit: int) -> dict:
        with self.db.session() as session:
            query = select(AICredential).where(AICredential.username == username)
            total = session.scalar(select(func.count()).select_from(query.subquery()))
            rows = session.scalars(
                query.order_by(AICredential.created_at.desc(), AICredential.id)
                .offset(offset)
                .limit(limit)
            )
            return {
                "items": [self.metadata(row) for row in rows],
                "total": total,
                "offset": offset,
                "limit": limit,
            }

    def revoke(self, username: str, credential_id: str):
        with self.db.session() as session:
            row = session.get(AICredential, credential_id)
            if not row or row.username != username:
                raise HTTPException(404, "凭据不存在")
            row.revoked = True
        self.audit(username, "credential.revoke", credential_id, "success")
        return {"id": credential_id, "status": "revoked"}

    def credential_principal(
        self, credential_id: str, scopes: list[str] | None = None
    ) -> Principal:
        with self.db.session() as session:
            row = session.get(AICredential, credential_id)
            if not row or row.revoked or row.expires_at <= now():
                raise HTTPException(401, "凭据无效、已撤销或已过期")
            granted = set(row.scopes) & self.allowed_scopes(row.username)
            return Principal(
                row.username, row.id, granted & set(scopes) if scopes is not None else granted
            )

    def authenticate(self, secret: str, allow_key: bool = True) -> Principal:
        if not secret:
            raise HTTPException(401, "缺少 Bearer 凭据")
        with self.db.session() as session:
            if allow_key and secret.startswith("psi_"):
                row = session.scalar(
                    select(AICredential).where(AICredential.digest == digest(secret))
                )
                if row:
                    return self.credential_principal(row.id)
            token = session.get(AISecret, digest(secret))
            if token and token.kind == "access" and token.expires_at > now() and token.payload.get("resource") == self.runtime.ai_base_url + "/mcp":
                return self.credential_principal(token.credential_id, token.payload["scopes"])
        raise HTTPException(401, "凭据无效、已撤销或已过期")

    def save_secret(
        self, kind: str, payload: dict, expires: int, credential_id: str | None = None
    ) -> str:
        secret = secrets.token_urlsafe(40)
        with self.db.session() as session:
            session.add(
                AISecret(
                    digest=digest(secret),
                    kind=kind,
                    payload=payload,
                    expires_at=expires,
                    credential_id=credential_id,
                )
            )
        return secret

    def issue_tokens(
        self, principal: Principal, client_id: str, resource: str, refresh: bool = False
    ) -> dict:
        principal = self.credential_principal(principal.credential_id, list(principal.scopes))
        with self.db.session() as session:
            credential = session.get(AICredential, principal.credential_id)
            expires = min(now() + 900, credential.expires_at)
            refresh_expires = min(now() + 30 * 86400, credential.expires_at)
        payload = {"client_id": client_id, "resource": resource, "scopes": sorted(principal.scopes)}
        token = self.save_secret("access", payload, expires, principal.credential_id)
        result = {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": expires - now(),
            "scope": " ".join(sorted(principal.scopes)),
        }
        if refresh:
            result["refresh_token"] = self.save_secret(
                "refresh", payload, refresh_expires, principal.credential_id
            )
        self.audit(principal.username, "token.issue", principal.credential_id, "success")
        return result

    def page(self, items: list, offset: int, limit: int) -> dict:
        return {
            "items": items[offset : offset + limit],
            "total": len(items),
            "offset": offset,
            "limit": limit,
        }

    def summary(self, p: Principal) -> dict:
        self.require(p, "status:read")
        return self.runtime.engine.summary()

    def checks(self, p: Principal, offset=0, limit=50) -> dict:
        self.require(p, "checks:read")
        return self.page(self.runtime.engine.summary()["states"], offset, limit)

    def incidents(self, p: Principal, offset=0, limit=50) -> dict:
        self.require(p, "status:read")
        # Pagination is applied by SQL in the shared engine.
        items, total = self.runtime.engine.incident_page(offset, limit)
        return {"items": items, "total": total, "offset": offset, "limit": limit}

    def history(self, p: Principal, days=30) -> dict:
        self.require(p, "history:read")
        return {"items": self.runtime.engine.history(days), "days": days}

    def tasks(self, p: Principal, offset=0, limit=50) -> dict:
        self.require(p, "tasks:read")
        with self.db.session() as session:
            query = select(AIJob).where(AIJob.username == p.username)
            total = session.scalar(select(func.count()).select_from(query.subquery()))
            rows = list(
                session.scalars(
                    query.order_by(AIJob.created_at.desc(), AIJob.id).offset(offset).limit(limit)
                )
            )
        return {
            "items": [self.job_dict(row) for row in rows],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    def job_dict(self, row: AIJob) -> dict:
        return {
            "id": row.id,
            "status": row.status,
            "created_at": iso(row.created_at),
            "finished_at": iso(row.finished_at),
            "result": row.result,
            "error": row.error,
            "progress": 100 if row.status in {"SUCCESS", "FAILED", "SKIPPED"} else None,
        }

    def task(self, p: Principal, job_id: str) -> dict:
        self.require(p, "tasks:read")
        with self.db.session() as session:
            row = session.get(AIJob, job_id)
            if not row or row.username != p.username:
                raise HTTPException(404, "任务不存在")
            return self.job_dict(row)

    def task_results(self, p: Principal, job_id: str, offset=0, limit=50) -> dict:
        self.require(p, "checks:read")
        job = self.task(p, job_id)
        run_id = (job["result"] or {}).get("run_id")
        items, total = self.runtime.engine.result_page(run_id, offset, limit) if run_id else ([], 0)
        return {"items": items, "total": total, "offset": offset, "limit": limit, "task_status": job["status"]}

    def idempotent(self, p: Principal, key: str, action: str, body: dict, execute) -> dict:
        if not 1 <= len(key) <= 128:
            raise HTTPException(422, "Idempotency-Key 长度须为 1–128")
        request_id = digest(p.username + ":" + action + ":" + key)
        fingerprint = digest(json.dumps(body, sort_keys=True))
        with self.db.session() as session:
            # Serialize writers across API/MCP and processes, including reservation of a job ID.
            session.execute(text("BEGIN IMMEDIATE"))
            previous = session.get(AIRequest, request_id)
            if previous:
                if previous.fingerprint != fingerprint:
                    raise HTTPException(409, "同一幂等键不能用于不同请求")
                return previous.response
            response = execute(session)
            session.add(
                AIRequest(
                    id=request_id, fingerprint=fingerprint, response=response, created_at=now()
                )
            )
        return response

    def submit(self, p: Principal, key: str) -> dict:
        self.require(p, "inspection:run")
        self.require(p, "tasks:read")

        def create(session):
            job_id = uuid.uuid4().hex
            session.add(
                AIJob(
                    id=job_id,
                    username=p.username,
                    credential_id=p.credential_id,
                    status="QUEUED",
                    created_at=now(),
                )
            )
            return {"id": job_id, "status_url": f"/api/v1/tasks/{job_id}"}

        response = self.idempotent(p, key, "inspection.run", {}, create)
        self.wake(response["id"])
        self.audit(p.username, "inspection.submit", response["id"], "accepted")
        return response

    def toggle(self, p: Principal, check_id: str, enabled: bool, confirm: bool, key: str) -> dict:
        self.require(p, "checks:write")
        if not confirm:
            raise HTTPException(422, "必须明确确认启用/停用操作及其影响")

        def change(session):
            try:
                self.runtime.store.set_check_enabled(check_id, enabled, session=session)
            except KeyError as exc:
                raise HTTPException(404, "巡检项不存在") from exc
            closed = (
                0
                if enabled
                else self.runtime.engine.close_open_incidents(check_id, session=session)
            )
            return {"id": check_id, "enabled": enabled, "closed_incidents": closed}

        result = self.idempotent(
            p, key, "check.toggle", {"id": check_id, "enabled": enabled}, change
        )
        self.audit(p.username, "check.toggle", check_id, "success")
        return result

    def wake(self, job_id: str):
        if job_id not in self.workers:
            self.workers[job_id] = asyncio.create_task(self.execute(job_id))
            self.workers[job_id].add_done_callback(lambda _: self.workers.pop(job_id, None))

    async def execute(self, job_id: str):
        with self.db.session() as session:
            row = session.get(AIJob, job_id)
            if not row or row.status != "QUEUED":
                return
            row.status = "RUNNING"
            credential_id, username = row.credential_id, row.username
        try:
            p = self.credential_principal(credential_id)
            self.require(p, "inspection:run")
            result = await self.runtime.engine.run_all(trigger="ai")
            state, error = result["status"], None
        except asyncio.CancelledError:
            # A running inspection may already have persisted results; never silently repeat it.
            state, result, error = (
                "FAILED",
                None,
                "服务停止导致任务中断；请检查结果后使用新幂等键重试",
            )
        except Exception:
            state, result, error = (
                "FAILED",
                None,
                "巡检执行失败或权限已失效；请联系管理员检查服务日志",
            )
        with self.db.session() as session:
            row = session.get(AIJob, job_id)
            row.status, row.result, row.error, row.finished_at = state, result, error, now()
        self.audit(username, "inspection.complete", job_id, state)

    def recover(self):
        with self.db.session() as session:
            rows = list(
                session.scalars(select(AIJob).where(AIJob.status.in_(["QUEUED", "RUNNING"])))
            )
            queued = [row.id for row in rows if row.status == "QUEUED"]
            for row in rows:
                if row.status == "RUNNING":
                    row.status, row.error, row.finished_at = (
                        "FAILED",
                        "服务重启中断；检查已有结果后使用新幂等键重试",
                        now(),
                    )
        for job_id in queued:
            self.wake(job_id)

    async def stop(self):
        workers = list(self.workers.values())
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
