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
from multiclaw.tenancy.context import TenantContext


SelfType = TypeVar("SelfType", bound="_BaseUnitOfWork")


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
        except BaseException:
            if self._tx is not None and self._tx.is_active:
                await self._tx.rollback()
            if self.conn is not None:
                await self.conn.close()
            self.conn = None
            self._tx = None
            raise

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            if self._tx is not None and self._tx.is_active:
                if exc_type is None:
                    await self._tx.commit()
                else:
                    await self._tx.rollback()
        finally:
            if self.conn is not None:
                await self.conn.close()
            self.conn = None
            self._tx = None

    def _bind_repositories(self) -> None:
        raise NotImplementedError


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

    def __init__(self, database: Database, context: TenantContext) -> None:
        super().__init__(database)
        self._context = context

    def _bind_repositories(self) -> None:
        assert self.conn is not None
        self.users = TenantUserRepository(self.conn, self._database.dialect, self._context)
        self.workspaces = WorkspaceRepository(self.conn, self._database.dialect, self._context)
