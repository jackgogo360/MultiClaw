from __future__ import annotations

from typing import Generic, TypeVar

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncTransaction

from multiclaw.storage.engine import Database
from multiclaw.storage.repositories.auth import (
    AuthUserRepository,
    TenantUserRepository,
    VerificationCodeRepository,
    WorkspaceRepository,
)
from multiclaw.storage.repositories.memory import MemoryRepository
from multiclaw.storage.repositories.sessions import SessionRepository
from multiclaw.tenancy.context import TenantContext


SelfType = TypeVar("SelfType", bound="_BaseUnitOfWork")


def _note_cleanup_error(primary: BaseException, phase: str, error: BaseException) -> None:
    primary.add_note(f"{phase} cleanup failed: {type(error).__name__}: {error}")


class _BaseUnitOfWork(Generic[SelfType]):
    def __init__(self, database: Database) -> None:
        self._database = database
        self.conn: AsyncConnection | None = None
        self._tx: AsyncTransaction | None = None

    async def __aenter__(self: SelfType) -> SelfType:
        try:
            self.conn = await self._database.engine.connect()
            self._tx = await self._database.dialect.begin_write(self.conn)
            self._bind_repositories()
            return self
        except BaseException as primary:
            await self._cleanup_after_failure(primary=primary, rollback_phase="rollback", close_phase="close")
            self.conn = None
            self._tx = None
            raise primary

    async def __aexit__(self, exc_type, exc, tb) -> None:
        primary: BaseException | None = exc
        try:
            if self._tx is not None and self._tx.is_active:
                if primary is None:
                    try:
                        await self._tx.commit()
                    except BaseException as error:
                        primary = error
                else:
                    await self._cleanup_after_failure(
                        primary=primary,
                        rollback_phase="rollback",
                        close_phase=None,
                    )
        finally:
            if primary is None:
                await self._close_without_primary()
            else:
                await self._cleanup_after_failure(
                    primary=primary,
                    rollback_phase=None,
                    close_phase="close",
                )
            self.conn = None
            self._tx = None
        if primary is not None:
            raise primary

    def _bind_repositories(self) -> None:
        raise NotImplementedError

    async def _cleanup_after_failure(
        self,
        *,
        primary: BaseException,
        rollback_phase: str | None,
        close_phase: str | None,
    ) -> None:
        if rollback_phase is not None and self._tx is not None and self._tx.is_active:
            try:
                await self._tx.rollback()
            except BaseException as error:
                _note_cleanup_error(primary, rollback_phase, error)
        if close_phase is not None and self.conn is not None:
            try:
                await self.conn.close()
            except BaseException as error:
                _note_cleanup_error(primary, close_phase, error)

    async def _close_without_primary(self) -> None:
        if self.conn is not None:
            await self.conn.close()


class AuthUnitOfWork(_BaseUnitOfWork["AuthUnitOfWork"]):
    users: AuthUserRepository
    verification_codes: VerificationCodeRepository

    def _bind_repositories(self) -> None:
        assert self.conn is not None
        self.users = AuthUserRepository(self.conn, self._database.dialect)
        self.verification_codes = VerificationCodeRepository(self.conn, self._database.dialect)


class TenantUnitOfWork(_BaseUnitOfWork["TenantUnitOfWork"]):
    users: TenantUserRepository
    workspaces: WorkspaceRepository
    sessions: SessionRepository
    memory: MemoryRepository

    def __init__(self, database: Database, context: TenantContext) -> None:
        super().__init__(database)
        self._context = context

    def _bind_repositories(self) -> None:
        assert self.conn is not None
        self.users = TenantUserRepository(self.conn, self._database.dialect, self._context)
        self.workspaces = WorkspaceRepository(self.conn, self._database.dialect, self._context)
        self.sessions = SessionRepository(self.conn, self._context, self._database.dialect)
        self.memory = MemoryRepository(self.conn, self._context, self._database.dialect)
