from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast
from uuid import uuid4

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from multiclaw.auth.models import UserRecord, VerificationCodeRecord, WorkspaceRecord, digests_match
from multiclaw.storage.dialect import MySQLDialect, SQLiteDialect
from multiclaw.storage.schema import users, verification_codes, workspaces
from multiclaw.tenancy.context import TenantContext


Dialect = SQLiteDialect | MySQLDialect


class BootstrapProbeError(RuntimeError):
    pass


def _note_cleanup_error(primary: BaseException, phase: str, error: BaseException) -> None:
    primary.add_note(f"{phase} cleanup failed: {type(error).__name__}: {error}")


@dataclass(slots=True)
class _ConnectionBoundRepository:
    _conn: AsyncConnection
    _dialect: Dialect

    @property
    def connection(self) -> AsyncConnection:
        return self._conn


class VerificationCodeRepository(_ConnectionBoundRepository):
    def __init__(self, conn: AsyncConnection, dialect: Dialect) -> None:
        super().__init__(conn, dialect)
        self._cleanup_registrar: Callable[[str, Callable[[], Awaitable[None]]], None] | None = None
        self._rate_limit_lock_name: str | None = None

    def attach_cleanup_registrar(
        self,
        registrar: Callable[[str, Callable[[], Awaitable[None]]], None],
    ) -> None:
        self._cleanup_registrar = registrar

    async def acquire_rate_limit_lock(
        self,
        *,
        email: str,
        purpose: str,
        timeout_seconds: int = 30,
    ) -> None:
        if self._rate_limit_lock_name is not None:
            return
        lock_name = await self._dialect.acquire_verification_codes_lock(
            self._conn,
            purpose=purpose,
            email=email,
            timeout_seconds=timeout_seconds,
        )
        if lock_name is None:
            return
        self._rate_limit_lock_name = lock_name
        if self._cleanup_registrar is None:
            return

        async def release_lock() -> None:
            assert self._rate_limit_lock_name is not None
            lock_name = self._rate_limit_lock_name
            self._rate_limit_lock_name = None
            await self._dialect.release_verification_codes_lock(self._conn, lock_name)

        self._cleanup_registrar("release verification code lock", release_lock)

    async def count_recent_codes(self, email: str, *, purpose: str, window_ms: int) -> int:
        result = await self._conn.execute(
            select(func.count())
            .select_from(verification_codes)
            .where(
                verification_codes.c.email == email,
                verification_codes.c.purpose == purpose,
                verification_codes.c.created_at > self._dialect.db_now_ms() - window_ms,
            )
        )
        return int(result.scalar_one())

    async def issue_code(
        self,
        *,
        email: str,
        purpose: str,
        code_digest: str,
        ttl_seconds: int,
    ) -> str:
        code_id = str(uuid4())
        now_ms = self._dialect.db_now_ms()
        await self._conn.execute(
            insert(verification_codes).values(
                id=code_id,
                email=email,
                code_digest=code_digest,
                purpose=purpose,
                expires_at=now_ms + ttl_seconds * 1000,
                used_at=None,
                created_at=now_ms,
            )
        )
        return code_id

    async def consume_latest_code(
        self,
        *,
        email: str,
        purpose: str,
        code_digest: str,
    ) -> VerificationCodeRecord | None:
        result = await self._conn.execute(
            select(
                verification_codes.c.id,
                verification_codes.c.email,
                verification_codes.c.code_digest,
                verification_codes.c.purpose,
                verification_codes.c.expires_at,
                verification_codes.c.used_at,
                verification_codes.c.created_at,
            )
            .where(
                verification_codes.c.email == email,
                verification_codes.c.purpose == purpose,
                verification_codes.c.used_at.is_(None),
                verification_codes.c.expires_at > self._dialect.db_now_ms(),
            )
            .order_by(verification_codes.c.created_at.desc(), verification_codes.c.id.desc())
            .limit(1)
        )
        row = result.mappings().first()
        if row is None:
            return None
        record = VerificationCodeRecord.from_row(row)
        if not digests_match(record.code_digest, code_digest):
            return None

        updated = await self._conn.execute(
            update(verification_codes)
            .where(
                verification_codes.c.id == record.id,
                verification_codes.c.used_at.is_(None),
                verification_codes.c.expires_at > self._dialect.db_now_ms(),
            )
            .values(used_at=self._dialect.db_now_ms())
        )
        if cast(int | None, updated.rowcount) != 1:
            return None
        return record

    async def db_now_ms(self) -> int:
        result = await self._conn.execute(select(self._dialect.db_now_ms()))
        return int(result.scalar_one())

    async def delete_expired_codes(self) -> int:
        result = await self._conn.execute(
            delete(verification_codes).where(
                verification_codes.c.expires_at <= self._dialect.db_now_ms()
            )
        )
        return int(result.rowcount or 0)


class AuthUserRepository(_ConnectionBoundRepository):
    async def get_by_id(self, user_id: str) -> UserRecord | None:
        result = await self._conn.execute(
            select(
                users.c.id,
                users.c.email,
                users.c.status,
                users.c.default_workspace_id,
                users.c.auth_epoch,
                users.c.created_at,
                users.c.updated_at,
            )
            .where(users.c.id == user_id)
            .limit(1)
        )
        row = result.mappings().first()
        return None if row is None else UserRecord.from_row(row)

    async def create_user_with_default_workspace(
        self,
        email: str,
        *,
        fail_after_workspace: bool = False,
    ) -> UserRecord:
        normalized_email = email.strip().lower()
        user_id = str(uuid4())
        workspace_id = str(uuid4())
        now_ms = self._dialect.db_now_ms()
        savepoint = await self._conn.begin_nested()

        try:
            await self._conn.execute(
                insert(users).values(
                    id=user_id,
                    email=normalized_email,
                    auth_epoch=0,
                    default_workspace_id=None,
                    status="active",
                    purge_after=None,
                    created_at=now_ms,
                    updated_at=now_ms,
                    disabled_at=None,
                    purge_requested_at=None,
                )
            )
            await self._conn.execute(
                insert(workspaces).values(
                    id=workspace_id,
                    tenant_id=user_id,
                    slug="default",
                    name="Default",
                    status="active",
                    created_at=now_ms,
                    updated_at=now_ms,
                )
            )

            if fail_after_workspace:
                raise BootstrapProbeError("fail_after_workspace")

            await self._conn.execute(
                update(users)
                .where(users.c.id == user_id)
                .values(default_workspace_id=workspace_id, updated_at=now_ms)
            )
            await savepoint.commit()
        except IntegrityError as primary:
            if not await self._rollback_savepoint(savepoint, primary):
                raise primary
            return await self._get_existing_bootstrapped_user(normalized_email)
        except BaseException as primary:
            await self._rollback_savepoint(savepoint, primary)
            raise primary

        return await self._get_existing_bootstrapped_user(normalized_email)

    async def _rollback_savepoint(
        self,
        savepoint,
        primary: BaseException | None,
    ) -> bool:
        if not savepoint.is_active:
            return True
        try:
            await savepoint.rollback()
        except BaseException as error:
            if primary is None:
                raise
            _note_cleanup_error(primary, "savepoint rollback", error)
            return False
        return True

    async def _get_existing_bootstrapped_user(self, email: str) -> UserRecord:
        result = await self._conn.execute(
            select(
                users.c.id,
                users.c.email,
                users.c.status,
                users.c.default_workspace_id,
                users.c.auth_epoch,
                users.c.created_at,
                users.c.updated_at,
            )
            .select_from(
                users.join(
                    workspaces,
                    (workspaces.c.tenant_id == users.c.id)
                    & (workspaces.c.id == users.c.default_workspace_id),
                )
            )
            .where(
                users.c.email == email,
                users.c.status == "active",
                users.c.default_workspace_id.is_not(None),
                workspaces.c.slug == "default",
                workspaces.c.status == "active",
            )
            .limit(1)
        )
        row = result.mappings().first()
        if row is None:
            raw_user = await self._conn.execute(
                select(users.c.id).where(users.c.email == email).limit(1)
            )
            if raw_user.scalar_one_or_none() is not None:
                raise RuntimeError("existing user is not fully bootstrapped")
            raise RuntimeError("user bootstrap did not persist")
        return UserRecord.from_row(row)


@dataclass(slots=True)
class TenantUserRepository(_ConnectionBoundRepository):
    _context: TenantContext

    async def get_current(self) -> UserRecord:
        result = await self._conn.execute(
            select(
                users.c.id,
                users.c.email,
                users.c.status,
                users.c.default_workspace_id,
                users.c.auth_epoch,
                users.c.created_at,
                users.c.updated_at,
            )
            .select_from(users.join(workspaces, workspaces.c.tenant_id == users.c.id))
            .where(
                users.c.id == self._context.tenant_id,
                workspaces.c.tenant_id == self._context.tenant_id,
                workspaces.c.id == self._context.workspace_id,
            )
            .limit(1)
        )
        row = result.mappings().one()
        return UserRecord.from_row(row)


@dataclass(slots=True)
class WorkspaceRepository(_ConnectionBoundRepository):
    _context: TenantContext

    async def get_current(self) -> WorkspaceRecord:
        result = await self._conn.execute(
            select(
                workspaces.c.id,
                workspaces.c.tenant_id,
                workspaces.c.slug,
                workspaces.c.name,
                workspaces.c.status,
                workspaces.c.created_at,
                workspaces.c.updated_at,
            )
            .where(
                workspaces.c.tenant_id == self._context.tenant_id,
                workspaces.c.id == self._context.workspace_id,
            )
            .limit(1)
        )
        row = result.mappings().one()
        return WorkspaceRecord.from_row(row)
