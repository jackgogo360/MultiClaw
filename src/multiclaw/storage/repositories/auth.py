from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from multiclaw.auth.models import UserRecord, WorkspaceRecord
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
    pass


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
